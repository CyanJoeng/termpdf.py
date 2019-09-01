#!/usr/bin/env python3
# vim:fileencoding=utf-8
"""\
Usage:
    termpdf.py [options] example.pdf

Options:
    -p n, --page-number n : open to page n
    -f n, --first-page n : set logical page number for page 1 to n
    --citekey key : bibtex citekey
    --nvim-listen-address path : path to nvim msgpack server
    -v, --version
    -h, --help
"""

__version__ = "0.1.1"
__license__ = "MIT"
__copyright__ = "Copyright (c) 2019"
__author__ = "David Sanson"
__url__ = "https://github.com/dsanson/termpdf.py"

__viewer_shortcuts__ = """\
Keys:
    j, down, space: forward [count] pages
    k, up:          back [count] pages
    l, right:       forward [count] sections
    h, left:        back [count] sections
    gg:             go to beginning of document
    G:              go to end of document
    [count]G:       go to page [count]
    v:              visual mode
    t:              table of contents 
    M:              show metadata
    u:              list URLs
    r:              rotate [count] quarter turns clockwise
    R:              rotate [count] quarter turns counterclockwise
    c:              toggle autocropping of margins
    a:              toggle alpha transparency
    i:              invert colors
    d:              darken using TINT_COLOR
    ctrl-r:         refresh
    q:              quit
"""

KITTYCMD = 'kitty --single-instance --instance-group=1' # open notes in a new OS window
TINT_COLOR = 'antiquewhite2'

import re
import array
import curses
import fcntl
import fitz
import os
import sys
import termios
import subprocess
import zlib
import shutil
import select
import pyperclip
from time import sleep, monotonic
from base64 import standard_b64encode
from collections import namedtuple
from math import ceil

URL_BROWSER_LIST = [
    'gnome-open',
    'gvfs-open',
    'xdg-open',
    'kde-open',
    'firefox',
    'w3m',
    'elinks',
    'lynx'
]

URL_BROWSER = None
if sys.platform == 'darwin':
    URL_BROWSER = 'open'
else:
    for i in URL_BROWSER_LIST:
        if shutil.which(i) is not None:
            URL_BROWSER = i
            break

# Class Definitions

class Document(fitz.Document):
    """
    An extension of the fitz.Document class, with extra attributes
    """
    def __init__(self, filename=None, filetype=None, rect=None, width=0, height=0, fontsize=12):
        fitz.Document.__init__(self, filename, None, filetype, rect, width, height, fontsize)
        self.filename = filename
        self.key = None
        self.page = 0
        self.prevpage = 0
        self.pages = self.pageCount - 1
        self.first_page_offset = 1
        self.chapter = 0
        self.rotation = 0
        self.fontsize = fontsize
        self.autocrop = False
        self.alpha = False
        self.invert = False
        self.tint = False
        self.tint_color = TINT_COLOR
        self.note_path = 'termpdf_notes.org'
        self.nvim = None
        self.nvim_listen_address = '/tmp/termpdf_nvim_bridge'
        self.page_states = [ Page_State(i) for i in range(0,self.pages + 1) ]

    def goto_page(self, p):
        # store prevpage 
        self.prevpage = self.page
        # delete prevpage
        # self.clear_page(self.prevpage)
        # set new page
        if p > self.pages:
            self.page = self.pages
        elif p < 0:
            self.page = 0
        else:
            self.page = p
    
    def goto_logical_page(self, p):
        p = self.logical_to_page(p)
        self.goto_page(p)

    def next_page(self, count=1):
        self.goto_page(self.page + count)

    def prev_page(self, count=1):
        self.goto_page(self.page - count)

    def goto_chap(self, n):
        toc = self.getToC()
        if n > len(toc):
            n = len(toc)
        elif n < 0:
            n = 0
        self.chapter = n
        try:
            self.goto_page(toc[n][2] - 1)
        except:
            self.goto_page(0)

    def current_chap(self):
        toc = self.getToC()
        p = self.page
        for i,ch in enumerate(toc):
           cp = ch[2] - 1
           if cp > p:
               return i - 1
        return len(toc)

    def next_chap(self, count=1):
        self.goto_chap(self.chapter + count)

    def prev_chap(self, count=1):
        self.goto_chap(self.chapter - count)

    def parse_pagelabels(self):
        from pdfrw import PdfReader, PdfWriter
        from pagelabels import PageLabels, PageLabelScheme
        
        reader = PdfReader(self.filename)
        labels = PageLabels.from_pdf(reader)
        print(labels)
        raise SystemExit

    def parse_pagelabels_pure(self):
        cat = self._getPDFroot().get_

        cat_str = self._getXrefString(cat)
        lines = cat_str.split('\n')
        labels = []
        for line in lines:
            match = re.search('/PageLabels',line)
            if re.match(r'.*/PageLabels.*', line):
                labels += [line]
        print(labels)
        raise SystemExit

    # TODO add support for logical page spec in PDF catalog
    def page_to_logical(self, p=None):
        if p == None:
            p = self.page
        return p + self.first_page_offset

    def logical_to_page(self, p=None):
        if not p:
            p = self.page
        return p - self.first_page_offset 

    def create_link(self):
        p = self.logical_page()
        if self.key:
            return '[{}, {}]'.format(self.key,p)
        else:
            return '[{}]'.format(p)

    def set_layout(self,scr,fontsize=None):
        pct = self.page / (self.pages)
        w = scr.width
        h = scr.height
        if fontsize:
            f = fontsize
            self.fontsize = f
        else:
            f = self.fontsize
        self.layout(width=w,height=h,fontsize=f)
        self.goto_page(round((self.pages) * pct))

    def mark_all_pages_stale(self):
        for s in self.page_states:
            s.stale = True

    def clear_page(self, p):
        cmd = {'a': 'd', 'd': 'a', 'i': p + 1}
        write_gr_cmd(cmd)

    def cells_to_pixels(self, scr, *coords):
        factor = self.page_states[self.page].factor
        l,t,_,_ = self.page_states[self.page].place
        pix_coords = []
        for coord in coords:
            col = coord[0]
            row = coord[1]
            x = (col - l) * scr.cell_width / factor
            y = (row - t) * scr.cell_height  / factor
            pix_coords.append((x,y))
        return pix_coords

    def pixels_to_cells(self, scr, *coords):
        factor = self.page_states[self.page].factor
        l,t,_,_ = self.page_states[self.page].place
        cell_coords = []
        for coord in coords:
            x = coord[0]
            y = coord[1]
            col = (x * factor + l * scr.cell_width) / scr.cell_width
            row = (y * factor + t * scr.cell_height) / scr.cell_height
            col = int(col)
            row = int(row)
            cell_coords.append((col,row))
        return cell_coords

    # get text that is inside a Rect
    def get_text_in_Rect(self, rect):
        from operator import itemgetter
        from itertools import groupby
        page = self.loadPage(self.page)
        words = page.getTextWords()
        mywords = [w for w in words if fitz.Rect(w[:4]) in rect]
        mywords.sort(key=itemgetter(3, 0))  # sort by y1, x0 of the word rect
        group = groupby(mywords, key=itemgetter(3))
        text = [] 
        for y1, gwords in group:
            text = text + [" ".join(w[4] for w in gwords)]
        return text

    # get text that intersects a Rect
    def get_text_intersecting_Rect(self, rect):
        from operator import itemgetter
        from itertools import groupby
        page = self.loadPage(self.page)
        words = page.getTextWords()
        mywords = [w for w in words if fitz.Rect(w[:4]).intersects(rect)]
        mywords.sort(key=itemgetter(3, 0))  # sort by y1, x0 of the word rect
        group = groupby(mywords, key=itemgetter(3))
        text = [] 
        for y1, gwords in group:
            text = text + [" ".join(w[4] for w in gwords)]
        return text



    def make_link(self):
        p = self.page_to_logical(self.page)
        if self.key: 
            return '[{}, {}]'.format(self.key, p)
        else:
            return '[{}]'.format(p)

    def auto_crop(self,page):
        blocks = page.getTextBlocks(images=True)

        if len(blocks) > 0:
            crop = fitz.Rect(blocks[0][:4])
        else:
            # don't try to crop empty pages
            crop = fitz.Rect(0,0,0,0)
        for block in blocks:
            b = fitz.Rect(block[:4])
            crop = crop | b

        return crop

    def display_page(self, scr, p, display=True):
        
        page = self.loadPage(p)
        page_state = self.page_states[p]
        
        if self.autocrop:
            page.setCropBox(page.MediaBox)
            crop = self.auto_crop(page)
            page.setCropBox(crop)

        elif self.isPDF:
            page.setCropBox(page.MediaBox)
           
        dw = scr.width
        dh = scr.height - scr.cell_height

        if self.rotation in [0,180]:
            pw = page.bound().width
            ph = page.bound().height
        else:
            pw = page.bound().height
            ph = page.bound().width
        
        # calculate zoom factor
        fx = dw / pw
        fy = dh / ph
        factor = min(fx,fy)
        self.page_states[p].factor = factor
    
        # calculate zoomed dimensions
        zw = factor * pw
        zh = factor * ph

        # calculate place in pixels, convert to cells
        pix_x = (dw / 2) - (zw / 2)
        pix_y = (dh / 2) - (zh / 2)
        l_col = int(pix_x / scr.cell_width)
        t_row = int(pix_y / scr.cell_height)
        r_col = l_col + int(zw / scr.cell_width)
        b_row = t_row + int(zh / scr.cell_height)
        place = (l_col, t_row, r_col, b_row)
        self.page_states[p].place = place

        # move cursor to place
        scr.set_cursor(l_col,t_row)

        # clear previous page
        # display image
        cmd = {'a': 'p', 'i': p + 1, 'z': -1}
        if page_state.stale: #or (display and not write_gr_cmd_with_response(cmd)):
            # get zoomed and rotated pixmap
            mat = fitz.Matrix(factor, factor)
            mat = mat.preRotate(self.rotation)
            pix = page.getPixmap(matrix = mat, alpha=self.alpha)
            
            if self.invert:
                pix.invertIRect()

            if self.tint:
                tint = fitz.utils.getColor(self.tint_color)
                red = int(tint[0] * 256)
                blue = int(tint[1] * 256)
                green = int(tint[2] * 256)
                pix.tintWith(red,blue,green)
            
            # build cmd to send to kitty
            cmd = {'i': p + 1, 't': 'd', 's': pix.width, 'v': pix.height}

            if self.alpha:
                cmd['f'] = 32
            else:
                cmd['f'] = 24

            # transfer the image
            write_chunked(cmd, pix.samples)

        if display:  
            # clear prevpage
            self.clear_page(self.prevpage)
            # display the image
            cmd = {'a': 'p', 'i': p + 1, 'z': -1}
            success = write_gr_cmd_with_response(cmd)
            if not success:
                self.page_states[p].stale = True
                bar.message = 'failed to load page ' + str(p+1)
                bar.update(self,scr)

        self.page_states[p].stale = False 

        scr.swallow_keys()

    def show_toc(self, scr, bar):

        toc = self.getToC()

        if not toc:
            bar.message = "No ToC available"
            return

        self.page_states[self.page ].stale = True
        self.clear_page(self.page)
        scr.clear()
        
        def init_pad(scr,toc):
            win, pad = scr.create_text_win(len(toc), 'Table of Contents')
            y,x = win.getbegyx()
            h,w = win.getmaxyx()
            span = []
            for i, ch in enumerate(toc):
                text = '{}{}'.format('  ' * (ch[0] - 1), ch[1])
                pad.addstr(i,0,text)
                span.append(len(text))
            return win,pad,y,x,h,w,span

        win,pad,y,x,h,w,span = init_pad(scr,toc)

        keys = shortcuts()
        index = self.current_chap()
        j = 0
       
        while True:
            for i, ch in enumerate(toc):
                attr = curses.A_REVERSE if index == i else curses.A_NORMAL
                pad.chgat(i, 0, span[i], attr)
            pad.refresh(j, 0, y + 3, x + 2, y + h - 2, x + w - 3)
            key = scr.stdscr.getch()
            
            if key in keys.REFRESH:
                scr.clear()
                scr.get_size()
                scr.init_curses()
                self.set_layout(scr)
                self.mark_all_pages_stale()
                init_pad(scr,toc)
            elif key in keys.QUIT:
                clean_exit(self,scr)
            elif key == 27 or key in keys.SHOW_TOC:
                scr.clear()
                return
            elif key in keys.NEXT_PAGE:
                index = min(len(toc) - 1, index + 1)
            elif key in keys.PREV_PAGE:
                index = max(0, index - 1)
            elif key in keys.OPEN:
                scr.clear()
                self.goto_page(toc[index][2] - 1)
                return
            
            if index > j + (h - 5):
                j += 1
            if index < j:
                j -= 1
            
    def show_meta(self, scr, bar):

        meta = self.metadata
        

        if not meta:
            bar.message = "No metadata available"
            return

        self.page_states[self.page].stale = True
        self.clear_page(self.page)
        scr.clear()
        
        def init_pad(scr,metadata):
            win, pad = scr.create_text_win(len(meta), 'Metadata')
            y,x = win.getbegyx()
            h,w = win.getmaxyx()
            span = []
            for i, mkey in enumerate(meta):
                text = '{}: {}'.format(mkey,meta[mkey])
                pad.addstr(i,0,text)
                span.append(len(text))
            return win,pad,y,x,h,w,span

        win,pad,y,x,h,w,span = init_pad(scr,meta)

        keys = shortcuts()
        index = 0
        j = 0
       
        while True:
            for i, mkey in enumerate(meta):
                attr = curses.A_REVERSE if index == i else curses.A_NORMAL
                pad.chgat(i, 0, span[i], attr)
            pad.refresh(j, 0, y + 3, x + 2, y + h - 2, x + w - 3)
            key = scr.stdscr.getch()
            
            if key in keys.REFRESH:
                scr.clear()
                scr.get_size()
                scr.init_curses()
                self.set_layout(scr)
                self.mark_all_pages_stale()
                init_pad(scr,meta)
            elif key in keys.QUIT:
                clean_exit(self,scr)
            elif key == 27 or key in keys.SHOW_META:
                scr.clear()
                return
            elif key in keys.NEXT_PAGE:
                index = min(len(meta) - 1, index + 1)
            elif key in keys.PREV_PAGE:
                index = max(0, index - 1)
            elif key in keys.OPEN:
                # TODO edit metadata, import metadata from bibtex
                pass
            
            if index > j + (h - 5):
                j += 1
            if index < j:
                j -= 1
   
    def goto_link(self,link):
        kind = link['kind']
        # 0 == no destination
        # 1 == internal link
        # 2 == uri
        # 3 == launch link
        # 5 == external pdf link
        if kind == 0:
            pass
        elif kind == 1:
            self.goto_page(link['page'])
        elif kind == 2:
            subprocess.run([URL_BROWSER, link['uri']], check=True)
        elif kind == 3:
            # not sure what these are
            pass
        elif kind == 5:
            # open external pdf in new buffer
            pass

    def show_urls(self, scr, bar):

        links = self[self.page].getLinks()

        urls = [link for link in links if 0 < link['kind'] < 3]

        if not urls:
            bar.message = "No urls on page"
            return

        self.page_states[self.page].stale = True
        self.clear_page(self.page)
        scr.clear()
        
        def init_pad(scr,urls):
            win, pad = scr.create_text_win(len(urls), 'URLs')
            y,x = win.getbegyx()
            h,w = win.getmaxyx()
            span = []
            for i, url in enumerate(urls):
                anchor_text = self.get_text_intersecting_Rect(url['from'])
                if len(anchor_text) > 0:
                    anchor_text = anchor_text[0]
                else:
                    anchor_text = ''
                if url['kind'] == 2:
                    link_text = url['uri']
                else:
                    link_text = url['page']

                text = '{}: {}'.format(anchor_text, link_text)
                pad.addstr(i,0,text)
                span.append(len(text))
            return win,pad,y,x,h,w,span

        win,pad,y,x,h,w,span = init_pad(scr,urls)

        keys = shortcuts()
        index = 0
        j = 0
       
        while True:
            for i, url in enumerate(urls):
                attr = curses.A_REVERSE if index == i else curses.A_NORMAL
                pad.chgat(i, 0, span[i], attr)
            pad.refresh(j, 0, y + 3, x + 2, y + h - 2, x + w - 3)
            key = scr.stdscr.getch()
            
            if key in keys.REFRESH:
                scr.clear()
                scr.get_size()
                scr.init_curses()
                self.set_layout(scr)
                self.mark_all_pages_stale()
                init_pad(scr,urls)
            elif key in keys.QUIT:
                clean_exit(self,scr)
            elif key == 27 or key in keys.SHOW_URLS:
                scr.clear()
                return
            elif key in keys.NEXT_PAGE:
                index = min(len(urls) - 1, index + 1)
            elif key in keys.PREV_PAGE:
                index = max(0, index - 1)
            elif key in keys.OPEN:
                self.goto_link(urls[index])
                # subprocess.run([URL_BROWSER, urls[index]['uri']], check=True)
                scr.clear()
                return
                 
            if index > j + (h - 5):
                j += 1
            if index < j:
                j -= 1
    
    def view_text(self, scr):
        pass

    def init_neovim_bridge(self):
        try:
            from pynvim import attach
        except:
            raise SystemExit('pynvim unavailable')
        try:
            self.nvim = attach('socket', path=self.nvim_listen_address)
        except:
            ncmd = 'env NVIM_LISTEN_ADDRESS={} nvim {}'.format(self.nvim_listen_address, self.note_path)
            try:
                os.system('{} {}'.format(KITTYCMD,ncmd))
            except:
                raise SystemExit('unable to open new kitty window')

            end = monotonic() + 5 # 5 second time out 
            while monotonic() < end:
                try:
                    self.nvim = attach('socket', path=self.nvim_listen_address)
                    break
                except:
                    # keep trying every tenth of a second
                    sleep(0.1)

    def send_to_neovim(self,text):
        try:
            self.nvim.api.strwidth('are you there?')
        except: 
            self.init_neovim_bridge()
        if not self.nvim:
            return 
        line = self.nvim.funcs.line('.')
        self.nvim.funcs.append(line, text)
        self.nvim.funcs.cursor(line + len(text), 0)


class Page_State:
    def __init__(self, p):
        self.number = p
        self.stale = True
        self.factor = (1,1)
        self.place = (0,0,40,40)
        self.crop = None

class screen:

    def __init__(self):
        self.rows = 0
        self.cols = 0
        self.width = 0
        self.height = 0
        self.cell_width = 0
        self.cell_height = 0
        self.stdscr = None

    def get_size(self):
        fd = sys.stdout
        buf = array.array('H', [0, 0, 0, 0])
        fcntl.ioctl(fd, termios.TIOCGWINSZ, buf)
        r,c,w,h = tuple(buf)
        cw = w // (c or 1)
        ch = h // (r or 1)
        self.rows = r
        self.cols = c
        self.width = w
        self.height = h
        self.cell_width = cw
        self.cell_height = ch

    def init_curses(self):
        os.environ.setdefault('ESCDELAY', '25')
        self.stdscr = curses.initscr()
        self.stdscr.clear()
        curses.noecho()
        curses.curs_set(0) 
        curses.mousemask(curses.REPORT_MOUSE_POSITION
            | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED
            | curses.BUTTON2_PRESSED | curses.BUTTON2_RELEASED
            | curses.BUTTON3_PRESSED | curses.BUTTON3_RELEASED
            | curses.BUTTON4_PRESSED | curses.BUTTON4_RELEASED
            | curses.BUTTON1_CLICKED | curses.BUTTON3_CLICKED
            | curses.BUTTON1_DOUBLE_CLICKED 
            | curses.BUTTON1_TRIPLE_CLICKED
            | curses.BUTTON2_DOUBLE_CLICKED 
            | curses.BUTTON2_TRIPLE_CLICKED
            | curses.BUTTON3_DOUBLE_CLICKED 
            | curses.BUTTON3_TRIPLE_CLICKED
            | curses.BUTTON4_DOUBLE_CLICKED 
            | curses.BUTTON4_TRIPLE_CLICKED
            | curses.BUTTON_SHIFT | curses.BUTTON_ALT
            | curses.BUTTON_CTRL)
        self.stdscr.keypad(True) # Handle our own escape codes for now

        # The first call to getch seems to clobber the statusbar.
        # So we make a dummy first call.
        self.stdscr.nodelay(True)
        self.stdscr.getch()
        self.stdscr.nodelay(False)

    def create_text_win(self, length, header):
        # calculate dimensions
        w = max(self.cols - 4, 60)
        h = self.rows - 2
        x = int(self.cols / 2 - w / 2)
        y = 1

        win = curses.newwin(h,w,y,x)
        win.box()
        win.addstr(1,2, '{:^{l}}'.format(header, l=(w-3)))
        
        self.stdscr.clear()
        self.stdscr.refresh()
        win.refresh()
        pad = curses.newpad(length,1000)
        pad.keypad(True)
        
        return win, pad

    def swallow_keys(self):
        self.stdscr.nodelay(True)
        k = self.stdscr.getch()
        end = monotonic() + 0.1
        while monotonic() < end:
            self.stdscr.getch()
        self.stdscr.nodelay(False)

    def clear(self):
        sys.stdout.buffer.write('\033[2J'.encode('ascii'))

    def set_cursor(self,c,r):
        if c > self.cols:
            c = self.cols
        elif c < 0:
            c = 0
        if r > self.rows:
            r = self.rows
        elif r < 0:
            r = 0
        sys.stdout.buffer.write('\033[{};{}f'.format(r, c).encode('ascii'))

    def place_string(self,c,r,string):
        self.set_cursor(c,r)
        sys.stdout.write(string)
        sys.stdout.flush()

class status_bar:

    def __init__(self):
        self.cols = 40
        self.rows = 1
        self.cmd = ' '
        self.message = ' '
        self.counter = ' '
        self.format = '{} {:^{me_w}} {}'
        self.bar = ''

    def update(self, doc, scr):
        p = doc.page_to_logical()
        pc = doc.page_to_logical(doc.pages)
        if pc == doc.pageCount:
            self.counter = '[{}/{}]'.format(p, pc)
        else:
            pf = doc.page_to_logical(0)
            self.counter = '[{}({}){}]'.format(pf,p,pc)
        w = self.cols = scr.cols
        cm_w = len(self.cmd)
        co_w = len(self.counter)
        me_w = w - cm_w - co_w - 2
        if len(self.message) > me_w:
            self.message = self.message[:me_w - 1] + '…' 
        self.bar = self.format.format(self.cmd, self.message, self.counter, me_w=me_w)
        scr.place_string(1,scr.rows,self.bar)

class shortcuts:

    def __init__(self):
        self.GOTO_PAGE        = {ord('G')}
        self.GOTO             = {ord('g')}
        self.NEXT_PAGE        = {ord('j'), curses.KEY_DOWN, ord(' ')}
        self.PREV_PAGE        = {ord('k'), curses.KEY_UP}
        self.NEXT_CHAP        = {ord('l'), curses.KEY_RIGHT}
        self.PREV_CHAP        = {ord('h'), curses.KEY_LEFT}
        self.HINTS            = {ord('f')}
        self.OPEN             = {curses.KEY_ENTER, curses.KEY_RIGHT, 10}
        self.SHOW_TOC         = {ord('t')}
        self.SHOW_META        = {ord('M')}
        self.SHOW_URLS        = {ord('u')}
        self.TOGGLE_TEXT_MODE = {ord('T')}
        self.ROTATE_CW        = {ord('r')}
        self.ROTATE_CCW       = {ord('R')}
        self.VISUAL_MODE      = {ord('v')}
        self.YANK             = {ord('y')}
        self.SEND_NOTE        = {ord('n')}
        self.TOGGLE_AUTOCROP  = {ord('c')}
        self.TOGGLE_ALPHA     = {ord('a')}
        self.TOGGLE_INVERT    = {ord('i')}
        self.TOGGLE_TINT      = {ord('d')}
        self.INC_FONT         = {ord('+')}
        self.DEC_FONT         = {ord('-')}
        self.REFRESH          = {18, curses.KEY_RESIZE}            # CTRL-R
        self.QUIT             = {3, ord('q')}
        self.DEBUG            = {ord('D')}

# Kitty graphics functions

def serialize_gr_command(cmd, payload=None):
   cmd = ','.join('{}={}'.format(k, v) for k, v in cmd.items())
   ans = []
   w = ans.append
   w(b'\033_G'), w(cmd.encode('ascii'))
   if payload:
      w(b';')
      w(payload)
   w(b'\033\\')
   return b''.join(ans)

def write_gr_cmd(cmd, payload=None):
    sys.stdout.buffer.write(serialize_gr_command(cmd, payload))
    sys.stdout.flush()

def write_gr_cmd_with_response(cmd, payload=None):
    write_gr_cmd(cmd, payload)
    resp = b''
    while resp[-2:] != b'\033\\':
        resp += sys.stdin.buffer.read(1)
    if b'OK' in resp:
        return True
    else:
        return False


def write_chunked(cmd, data):
    if cmd['f'] != 100:
        data = zlib.compress(data)
        cmd['o'] = 'z'
    data = standard_b64encode(data)
    while data:
        chunk, data = data[:4096], data[4096:]
        m = 1 if data else 0
        cmd['m'] = m
        write_gr_cmd(cmd, chunk)
        cmd.clear()



# Command line helper functions

def print_version():
    print(__version__)
    print(__license__, 'License')
    print(__copyright__, __author__)
    print(__url__)
    raise SystemExit


def print_help():
    print(__doc__.rstrip())
    print()
    print(__viewer_shortcuts__)
    raise SystemExit()

def parse_args(args):
    files = []
    opts = {} 
    if len(args) == 1:
        args = args + ['-h']
    args = args[1:]

    if len({'-h', '--help'} & set(args)) != 0:
        print_help()
    elif len({'-v', '--version'} & set(args)) != 0:
        print_version()
    
    skip = False
    for i,arg in enumerate(args):
        if skip:
            skip = not skip
        elif arg in {'-p', '--page-number'}:
            try:
                opts['page'] = int(args[i + 1]) - 1
                skip = True
            except:
                raise SystemExit('No valid page number specified')
        elif arg in {'-f', '--first-page'}:
            try:
                opts['first_page_offset'] = int(args[i + 1])
                skip = True
            except:
                raise SystemExit('No valid first page specified')
        elif arg in {'--nvim-listen-address'}:
            try:
                opts['nvim_listen_address'] = args[i + 1]
                skip = True
            except:
                raise SystemExit('No address specified')
        elif arg in {'--citekey'}:
            try:
                opts['key'] = args[i + 1]
                skip = True
            except:
                raise SystemExit('No citekey specified')
        elif os.path.isfile(arg):
            files = files + [arg]
        elif re.match('^-', arg):
            raise SystemExit('Unknown option: ' + arg)
        else:
            raise SystemExit('Can\'t open file: ' + arg)

    return files, opts


def clean_exit(doc, scr, mes=''):

    # close curses
    scr.stdscr.keypad(False)
    curses.echo()
    curses.curs_set(1)
    curses.endwin()
    
    # close the document
    doc.close()

    raise SystemExit(mes)



def get_text_in_rows(doc, scr, selection):
    l,t,r,b = doc.page_states[doc.page].place
    top = (l,t + selection[0] - 1)
    bottom = (r,t + selection[1])
    top_pix, bottom_pix = doc.cells_to_pixels(scr,top,bottom)
    rect = fitz.Rect(top_pix, bottom_pix)
    select_text = doc.get_text_in_Rect(rect)
    link = doc.make_link()
    select_text = select_text + [link]
    return (' '.join(select_text))

# Viewer functions

def visual_mode(doc,scr,bar):
    l,t,r,b = doc.page_states[doc.page].place
    
    width = (r - l) + 1

    def highlight_row(row, fill='▒', color='yellow'):
        if color == 'yellow':
            cc = 33
        elif color == 'blue':
            cc = 34
        elif color == 'none':
            cc = 0

        fill = fill[0] * width

        scr.set_cursor(l,row)
        sys.stdout.buffer.write('\033[{}m'.format(cc).encode('ascii'))
        #sys.stdout.buffer.write('\033[{}m'.format(cc + 10).encode('ascii'))
        sys.stdout.write(fill)
        sys.stdout.flush()
        sys.stdout.buffer.write(b'\033[0m')
        sys.stdout.flush()

    def unhighlight_row(row):
        # scr.set_cursor(l,row)
        # sys.stdout.write(' ' * width)
        # sys.stdout.flush()
        highlight_row(row,fill=' ',color='none')

    def highlight_selection(selection, fill='▒', color='blue'):
        a = min(selection)
        b = max(selection)
        for r in range(a,b+1):
            highlight_row(r,fill,color)

    def unhighlight_selection(selection):
        highlight_selection(selection,fill=' ',color='none')

    current_row = t
    select = False
    selection = [current_row,current_row]
    count_string = '' 

    while True:
       
        bar.cmd = count_string
        bar.update(doc,scr)
        unhighlight_selection([t,b])
        if select:
            highlight_selection(selection,color='blue')
        else:
            highlight_selection(selection,color='yellow')

        if count_string == '':
            count = 1
        else:
            count = int(count_string)

        keys = shortcuts() 
        key = scr.stdscr.getch()
      
        if key in range(48,58): #numerals
            count_string = count_string + chr(key)

        elif key in keys.QUIT:
            clean_exit(doc,scr)

        elif key == 27 or key in keys.VISUAL_MODE:
            unhighlight_selection([t,b])
            return

        elif key in {ord('s')}:
            if select:
                select = False
            else:
                select = True
            selection = [current_row, current_row]
            count_string = ''

        elif key in keys.NEXT_PAGE:
            current_row += count 
            current_row = min(current_row,b)
            if select:
                selection[1] = current_row
            else:
                selection = [current_row,current_row]
            count_string = ''

        elif key in keys.PREV_PAGE:
            current_row -= count 
            current_row = max(current_row,t)
            if select:
                selection[1] = current_row
            else:
                selection = [current_row,current_row]
            count_string = ''

        elif key in keys.GOTO_PAGE:
            current_row = b
            if select:
                selection[1] = current_row
            else:
                selection = [current_row,current_row]
            count_string = ''

        elif key in keys.GOTO:
            current_row = t
            if select:
                selection[1] = current_row
            else:
                selection = [current_row,current_row]
            count_string = ''

        elif key in keys.YANK:
            if selection == [None,None]:
                selection = [current_row, current_row]
            selection.sort()
            select_text = get_text_in_rows(doc,scr,selection)
            pyperclip.copy(select_text)
            unhighlight_selection([t,b])
            bar.message = 'copied'
            return

        elif key in keys.SEND_NOTE:
            if selection == [None,None]:
                selection = [current_row, current_row]
            selection.sort()
            select_text = get_text_in_rows(doc,scr,selection)
            doc.send_to_neovim(select_text)
            unhighlight_selection([t,b])
            return



def view(doc, scr):

    scr.get_size()
    scr.init_curses()

    bar = status_bar()
    if doc.key:
        bar.message = doc.key

    count_string = ""
    stack = [0]
    keys = shortcuts() 

    while True:

        bar.cmd = ''.join(map(chr,stack))
        bar.update(doc, scr)
        doc.display_page(scr,doc.page)

        if count_string == "":
            count = 1
        else:
            count = int(count_string)

        key = scr.stdscr.getch()

        if key in range(48,257): #printable characters
            stack.append(key)
        
        if key in keys.REFRESH:
            scr.clear()
            scr.get_size()
            scr.init_curses()
            doc.set_layout(scr)
            doc.mark_all_pages_stale()

        elif key == 27:
            # quash stray escape codes
            scr.swallow_keys()
            count_string = ""
            stack = [0]

        elif key in range(48,58): #numerals
            count_string = count_string + chr(key)

        elif key in keys.QUIT:
            clean_exit(doc, scr)

        elif key in keys.GOTO_PAGE:
            if count_string == "":
                p = doc.page_to_logical(doc.pages)
            else:
                p = count
            doc.goto_logical_page(p)
            count_string = ""
            stack = [0]

        elif key in keys.NEXT_PAGE:
            doc.next_page(count)
            count_string = ""
            stack = [0]

        elif key in keys.PREV_PAGE:
            doc.prev_page(count)
            count_string = ""
            stack = [0]

        elif key in keys.NEXT_CHAP:
            doc.next_chap(count)
            count_string = ""
            stack = [0]

        elif key in keys.PREV_CHAP:
            doc.prev_chap(count)
            count_string = ""
            stack = [0]

        elif stack[0] in keys.GOTO and key in keys.GOTO:
            doc.goto_page(0)
            count_string = ""
            stack = [0]

        elif key in keys.ROTATE_CW:
            doc.rotation = (doc.rotation + 90 * count) % 360
            doc.mark_all_pages_stale()
            count_string = ''
            stack = [0]

        elif key in keys.ROTATE_CCW:
            doc.rotation = (doc.rotation - 90 * count) % 360
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]

        elif key in keys.TOGGLE_AUTOCROP:
            doc.autocrop = not doc.autocrop
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]

        elif key in keys.TOGGLE_ALPHA:
            doc.alpha = not doc.alpha
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]

        elif key in keys.TOGGLE_INVERT:
            doc.invert = not doc.invert
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]
        
        elif key in keys.TOGGLE_TINT:
            doc.tint = not doc.tint
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]

        elif key in keys.SHOW_TOC:
            doc.show_toc(scr,bar)
            count_string = ""
            stack = [0]

        elif key in keys.SHOW_META:
            doc.show_meta(scr,bar)
            count_string = ""
            stack = [0]
        
        elif key in keys.SHOW_URLS:
            doc.show_urls(scr,bar)
            count_string = ""
            stack = [0]

        elif key in keys.TOGGLE_TEXT_MODE:
            doc.view_text(scr)
            count_string = ""
            stack = [0]
       
        elif key in keys.INC_FONT:
            doc.set_layout(scr,doc.fontsize + count * 2)
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]
        
        elif key in keys.DEC_FONT:
            doc.set_layout(scr,doc.fontsize - count * 2)
            doc.mark_all_pages_stale()
            count_string = ""
            stack = [0]

        elif key in keys.VISUAL_MODE:
            visual_mode(doc,scr,bar)
            count_string = ""
            stack = [0]

        elif key in keys.SEND_NOTE:
            text = doc.make_link()
            doc.send_to_neovim(text)
            count_string = ""
            stack = [0]

        elif key in keys.DEBUG:
            #doc.parse_pagelabels()
            # print(doc[doc.page].firstAnnot)
            # sleep(1)
            pass

def main(args=sys.argv):

    if not sys.stdin.isatty():
        raise SystemExit('Not an interactive tty')

    scr = screen()
    scr.get_size()

    if scr.width == 0:
        raise SystemExit(
            'Terminal does not support reporting screen sizes via the TIOCGWINSZ ioctl'
        )

    files, opts = parse_args(args)
    
    try:
        doc = Document(files[0])
    except:
        raise SystemExit('Unable to open ' + files[0])

    for key in opts:
        setattr(doc, key, opts[key])
  
    # normalize page number
    doc.goto_page(doc.page)

    view(doc, scr) 

if __name__ == '__main__':
    main()

