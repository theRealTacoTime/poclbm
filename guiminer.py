#!/usr/bin/python

"""GUIMiner - graphical frontend to Scrypt miners.

Currently supports:
- reaper
- cgminer
- cudaminer

Copyright 2011-2012 Chris MacLeod
Copyright 2012 TacoTime
This program is released under the GNU GPL. See LICENSE.txt for details.
"""

import os, sys, subprocess, errno, re, threading, logging, time, httplib, urllib, distutils.dir_util
print sys.path
import wx
import json
import collections
import pyopencl

from _winreg import (
    CloseKey, OpenKey, QueryValueEx, SetValueEx,
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE,
    KEY_ALL_ACCESS, KEY_READ, REG_EXPAND_SZ, REG_SZ
)

"""
Begin startup processes
"""

ON_POSIX = 'posix' in sys.builtin_module_names

try:
    import win32api, win32con, win32process
except ImportError:
    pass

from wx.lib.agw import flatnotebook as fnb
from wx.lib.agw import hyperlink
from wx.lib.newevent import NewEvent

__version__ = 'v0.04'

STARTUP_PATH = os.getcwd()

"""
End startup processes
"""

def get_module_path(): # Redundant with os.getcwd() at opening; not needed?  Tacotime
    """Return the folder containing this script (or its .exe)."""
    module_name = sys.executable if hasattr(sys, 'frozen') else __file__
    abs_path = os.path.abspath(module_name)
    return os.path.dirname(abs_path)

USE_MOCK = '--mock' in sys.argv
# Set up localization; requires the app to be created
app = wx.PySimpleApp(0)
wx.InitAllImageHandlers()

_ = wx.GetTranslation

LANGUAGES = {
    "Chinese Simplified": wx.LANGUAGE_CHINESE_SIMPLIFIED,
    "Dutch": wx.LANGUAGE_DUTCH,
    "English": wx.LANGUAGE_ENGLISH,
    "Esperanto": wx.LANGUAGE_ESPERANTO,
    "French": wx.LANGUAGE_FRENCH,
    "German": wx.LANGUAGE_GERMAN,
    "Hungarian": wx.LANGUAGE_HUNGARIAN,
    "Italian": wx.LANGUAGE_ITALIAN,
    "Portuguese": wx.LANGUAGE_PORTUGUESE,
    "Russian": wx.LANGUAGE_RUSSIAN,
    "Spanish": wx.LANGUAGE_SPANISH,
}
LANGUAGES_REVERSE = dict((v, k) for (k, v) in LANGUAGES.items())

DONATION_ADDRESS = "LiK1rotC2tNYNRbRfW2xsKLYJvKhQ3PwTN"
locale = None
language = None
def update_language(new_language):
    global locale, language
    language = new_language
    if locale:
        del locale

    locale = wx.Locale(language)
    if locale.IsOk():
        locale.AddCatalogLookupPathPrefix(os.path.join(get_module_path(), "locale"))
        locale.AddCatalog("guiminer")
    else:
        locale = None

def load_language():
    language_config = os.path.join(get_module_path(), 'default_language.ini')
    language_data = dict()
    if os.path.exists(language_config):
        with open(language_config) as f:
            language_data.update(json.load(f))
    language_str = language_data.get('language', "English")
    update_language(LANGUAGES.get(language_str, wx.LANGUAGE_ENGLISH))

def save_language():
    language_config = os.path.join(get_module_path(), 'default_language.ini')
    language_str = LANGUAGES_REVERSE.get(language)
    with open(language_config, 'w') as f:
        json.dump(dict(language=language_str), f)

load_language()

ABOUT_TEXT = _(
"""GUIMiner

Version: %(version)s
Scrypt mod by TacoTime
GUI by Chris 'Kiv' MacLeod
Original poclbm miner by m0mchil
Original rpcminer by puddinpop

Get the source code or file issues at GitHub:
    https://github.com/Kiv/poclbm

If you enjoyed this software, support its development
by donating to:

%(address)s

Even a single Litecoin is appreciated and helps motivate
further work on this software.
""")

# Translatable strings that are used repeatedly
STR_NOT_STARTED = _("Not started")
STR_STARTING = _("Starting")
STR_STOPPED = _("Stopped")
STR_PAUSED = _("Paused")
STR_START_MINING = _("Start")
STR_STOP_MINING = _("Stop")
STR_REFRESH_BALANCE = _("Refresh balance")
STR_CONNECTION_ERROR = _("Connection error")
STR_USERNAME = _("Username:")
STR_PASSWORD = _("Password:")
STR_QUIT = _("Quit this program")
STR_ABOUT = _("Show about dialog")

# Alternate backends that we know how to call
SUPPORTED_BACKENDS = [
    "rpcminer-4way.exe",
    "rpcminer-cpu.exe",
    "rpcminer-cuda.exe",
    "rpcminer-opencl.exe",
#    "phoenix.py",
#    "phoenix.exe",
    "bitcoin-miner.exe"
]

USER_AGENT = "guiminer/" + __version__

# Time constants
SAMPLE_TIME_SECS = 3600
REFRESH_RATE_MILLIS = 2000

# Layout constants
LBL_STYLE = wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL
BTN_STYLE = wx.ALIGN_CENTER_HORIZONTAL | wx.ALL

# Events sent from the worker threads
(UpdateHashRateEvent, EVT_UPDATE_HASHRATE) = NewEvent()
(UpdateAcceptedEvent, EVT_UPDATE_ACCEPTED) = NewEvent()
(ReaperAttributeUpdate, EVT_REAPER_ATTRIBUTE_UPDATE) = NewEvent()
(UpdateAcceptedReaperEvent, EVT_UPDATE_REAPER_ACCEPTED) = NewEvent()
(UpdateSoloCheckEvent, EVT_UPDATE_SOLOCHECK) = NewEvent()
(UpdateStatusEvent, EVT_UPDATE_STATUS) = NewEvent()

# Used in class CgListenerThread(MinerListenerThread) and ReaperListenerThread(MinerListenerThread)?
non_decimal = re.compile(r'[^\d.]+')

# Utility functions
def merge_whitespace(s):
    """Combine multiple whitespace characters found in s into one."""
    s = re.sub(r"( +)|\t+", " ", s)
    return s.strip()

def get_opencl_devices():
    """Return a list of available OpenCL devices.

    Raises ImportError if OpenCL is not found.
    Raises IOError if no OpenCL devices are found.
    """
    device_strings = []
    platforms = pyopencl.get_platforms() #@UndefinedVariable
    for i, platform in enumerate(platforms):
        devices = platform.get_devices()
        for j, device in enumerate(devices):
            device_strings.append('[%d-%d] %s' % 
                (i, j, merge_whitespace(device.name)[:25]))
    if len(device_strings) == 0:
        raise IOError
    return device_strings

def get_icon_bundle():
    """Return the Bitcoin program icon bundle."""
    return wx.IconBundleFromFile(os.path.join(get_module_path(), "logo.ico"), wx.BITMAP_TYPE_ICO)

def get_taskbar_icon():
    """Return the taskbar icon.

    This works around Window's annoying behavior of ignoring the 16x16 image
    and using nearest neighbour downsampling on the 32x32 image instead."""
    ib = get_icon_bundle()
    return ib.GetIcon((16, 16))

def mkdir_p(path):
    """If the directory 'path' doesn't exist, create it. Same as mkdir -p."""
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise

def add_tooltip(widget, text):
    """Add a tooltip to widget with the specified text."""
    tooltip = wx.ToolTip(text)
    widget.SetToolTip(tooltip)

def format_khash(rate):
    """Format rate for display. A rate of 0 means just connected."""
    if rate > 10 ** 6:
        return _("%.3f Ghash/s") % (rate / 1000000.)
    if rate > 10 ** 3:
        return _("%.1f Mhash/s") % (rate / 1000.)
    elif rate == 0:
        return _("Connecting...")
    elif rate == -0.0000001:
        return _("Proxy connected")
    else:
        return _("%d khash/s") % rate

def format_balance(amount):
    """Format a quantity of Bitcoins in BTC."""
    return "%.3f BTC" % float(amount)

def init_logger():
    """Set up and return the logging object and custom formatter."""
    logger = logging.getLogger("poclbm-gui")
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(
        os.path.join(get_module_path(), 'guiminer.log'), 'w')
    formatter = logging.Formatter("%(asctime)s: %(message)s",
                                  "%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger, formatter

logger, formatter = init_logger()

def http_request(hostname, *args, **kwargs):
    """Do a HTTP request and return the response data."""
    conn_cls = httplib.HTTPSConnection if kwargs.get('use_https') else httplib.HTTPConnection        
    conn = conn_cls(hostname) 
    try:        
        logger.debug(_("Requesting balance: %(request)s"), dict(request=args))
        conn.request(*args)
        response = conn.getresponse()
        data = response.read()
        logger.debug(_("Server replied: %(status)s, %(data)s"),
                     dict(status=str(response.status), data=data))
        return response, data
    finally:
        conn.close()

def get_process_affinity(pid):
    """Return the affinity mask for the specified process."""
    flags = win32con.PROCESS_QUERY_INFORMATION
    handle = win32api.OpenProcess(flags, 0, pid)
    return win32process.GetProcessAffinityMask(handle)[0]

def set_process_affinity(pid, mask):
    """Set the affinity for process to mask."""
    flags = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_SET_INFORMATION
    handle = win32api.OpenProcess(flags, 0, pid)
    win32process.SetProcessAffinityMask(handle, mask)

def find_nth(haystack, needle, n):
    """Return the index of the nth occurrence of needle in haystack."""
    start = haystack.find(needle)
    while start >= 0 and n > 1:
        start = haystack.find(needle, start + len(needle))
        n -= 1
    return start

class ConsolePanel(wx.Panel):
    """Panel that displays logging events.

    Uses with a StreamHandler to log events to a TextCtrl. Thread-safe.
    """
    def __init__(self, parent, n_max_lines):
        wx.Panel.__init__(self, parent, -1)
        self.parent = parent
        self.n_max_lines = n_max_lines

        vbox = wx.BoxSizer(wx.VERTICAL)
        style = wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL
        self.text = wx.TextCtrl(self, -1, "", style=style)
        vbox.Add(self.text, 1, wx.EXPAND)
        self.SetSizer(vbox)

        self.handler = logging.StreamHandler(self)

        formatter = logging.Formatter("%(asctime)s: %(message)s",
                                      "%Y-%m-%d %H:%M:%S")
        self.handler.setFormatter(formatter)
        logger.addHandler(self.handler)

    def on_focus(self):
        """On focus, clear the status bar."""
        self.parent.statusbar.SetStatusText("", 0)
        self.parent.statusbar.SetStatusText("", 1)

    def on_close(self):
        """On closing, stop handling logging events."""
        logger.removeHandler(self.handler)

    def append_text(self, text):
        self.text.AppendText(text)
        lines_to_cut = self.text.GetNumberOfLines() - self.n_max_lines 
        if lines_to_cut > 0:
            contents = self.text.GetValue()
            position = find_nth(contents, '\n', lines_to_cut)
            self.text.ChangeValue(contents[position + 1:])                        

    def write(self, text):
        """Forward logging events to our TextCtrl."""
        wx.CallAfter(self.append_text, text)


class SummaryPanel(wx.Panel):
    """Panel that displays a summary of all miners."""

    def __init__(self, parent):
        wx.Panel.__init__(self, parent, -1)
        self.parent = parent
        self.timer = wx.Timer(self)
        self.timer.Start(REFRESH_RATE_MILLIS)
        self.Bind(wx.EVT_TIMER, self.on_timer)

        flags = wx.ALIGN_CENTER_HORIZONTAL | wx.ALL
        border = 5
        self.column_headers = [
            (wx.StaticText(self, -1, _("Miner")), 0, flags, border),
            (wx.StaticText(self, -1, _("Speed")), 0, flags, border),
            (wx.StaticText(self, -1, _("Accepted")), 0, flags, border),
            (wx.StaticText(self, -1, _("Stale")), 0, flags, border),
            (wx.StaticText(self, -1, _("Start/Stop")), 0, flags, border),
            (wx.StaticText(self, -1, _("Autostart")), 0, flags, border),
        ]
        font = wx.SystemSettings_GetFont(wx.SYS_DEFAULT_GUI_FONT)
        font.SetUnderlined(True)
        for st in self.column_headers:
            st[0].SetFont(font)

        self.grid = wx.FlexGridSizer(0, len(self.column_headers), 2, 2)

        self.grid.AddMany(self.column_headers)
        self.add_miners_to_grid()

        self.grid.AddGrowableCol(0)
        self.grid.AddGrowableCol(1)
        self.grid.AddGrowableCol(2)
        self.grid.AddGrowableCol(3)
        self.SetSizer(self.grid)

    def add_miners_to_grid(self):
        """Add a summary row for each miner to the summary grid."""

        # Remove any existing widgets except the column headers.
        for i in reversed(range(len(self.column_headers), len(self.grid.GetChildren()))):
            self.grid.Hide(i)
            self.grid.Remove(i)

        for p in self.parent.profile_panels:
            p.clear_summary_widgets()
            self.grid.AddMany(p.get_summary_widgets(self))

        self.grid.Layout()

    def on_close(self):
        self.timer.Stop()

    def on_timer(self, event=None):
        """Whenever the timer goes off, fefresh the summary data."""
        if self.parent.nb.GetSelection() != self.parent.nb.GetPageIndex(self):
            return

        for p in self.parent.profile_panels:
            p.update_summary()

        self.parent.statusbar.SetStatusText("", 0) # TODO: show something
        total_rate = sum(p.last_rate for p in self.parent.profile_panels
                         if p.is_mining)
        if any(p.is_mining for p in self.parent.profile_panels):
            self.parent.statusbar.SetStatusText(format_khash(total_rate), 1)
        else:
            self.parent.statusbar.SetStatusText("", 1)

    def on_focus(self):
        """On focus, show the statusbar text."""
        self.on_timer()

class GUIMinerTaskBarIcon(wx.TaskBarIcon):
    """Taskbar icon for the GUI.

    Shows status messages on hover and opens on click.
    """
    TBMENU_RESTORE = wx.NewId()
    TBMENU_PAUSE = wx.NewId()
    TBMENU_CLOSE = wx.NewId()
    TBMENU_CHANGE = wx.NewId()
    TBMENU_REMOVE = wx.NewId()

    def __init__(self, frame):
        wx.TaskBarIcon.__init__(self)
        self.frame = frame
        self.icon = get_taskbar_icon()
        self.timer = wx.Timer(self)
        self.timer.Start(REFRESH_RATE_MILLIS)
        self.is_paused = False
        self.SetIcon(self.icon, "GUIMiner")
        self.imgidx = 1
        self.Bind(wx.EVT_TASKBAR_LEFT_DCLICK, self.on_taskbar_activate)
        self.Bind(wx.EVT_MENU, self.on_taskbar_activate, id=self.TBMENU_RESTORE)
        self.Bind(wx.EVT_MENU, self.on_taskbar_close, id=self.TBMENU_CLOSE)
        self.Bind(wx.EVT_MENU, self.on_pause, id=self.TBMENU_PAUSE)
        self.Bind(wx.EVT_TIMER, self.on_timer)

    def CreatePopupMenu(self):
        """Override from wx.TaskBarIcon. Creates the right-click menu."""
        menu = wx.Menu()
        menu.AppendCheckItem(self.TBMENU_PAUSE, _("Pause all"))
        menu.Check(self.TBMENU_PAUSE, self.is_paused)
        menu.Append(self.TBMENU_RESTORE, _("Restore"))
        menu.Append(self.TBMENU_CLOSE, _("Close"))
        return menu

    def on_taskbar_activate(self, evt):
        if self.frame.IsIconized():
            self.frame.Iconize(False)
        if not self.frame.IsShown():
            self.frame.Show(True)
        self.frame.Raise()

    def on_taskbar_close(self, evt):
        wx.CallAfter(self.frame.Close, force=True)

    def on_timer(self, event):
        """Refresh the taskbar icon's status message."""
        objs = self.frame.profile_panels
        if objs:
            text = '\n'.join(p.get_taskbar_text() for p in objs)
            self.SetIcon(self.icon, text)

    def on_pause(self, event):
        """Pause or resume the currently running miners."""
        self.is_paused = event.Checked()
        for miner in self.frame.profile_panels:
            if self.is_paused:
                miner.pause()
            else:
                miner.resume()

def nonBlockRead(output):
    fd = output.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    try:
        return output.read()
    except:
        return ''
                
class MinerListenerThread(threading.Thread):
    LINES = ["""
        (r"Target =|average rate|Sending to server|found hash|connected to|Setting server",
            lambda _: None), # Just ignore lines like these
        (r"accepted|\"result\":\s*true",
            lambda _: UpdateAcceptedEvent(accepted=True)),
        (r"invalid|stale|rejected", lambda _:
            UpdateAcceptedEvent(accepted=False)),     
        (r"(\d+)\s*Kh/s", lambda match:
            UpdateHashRateEvent(rate=float(match.group(1)))),
        (r"(\d+\.\d+)\s*MH/s", lambda match:
            UpdateHashRateEvent(rate=float(match.group(1)) * 1000)),
        (r"(\d+\.\d+)\s*Mhash/s", lambda match:
            UpdateHashRateEvent(rate=float(match.group(1)) * 1000)),
        (r"(\d+)\s*Mhash/s", lambda match:
            UpdateHashRateEvent(rate=int(match.group(1)) * 1000)),
        (r"checking (\d+)", lambda _:
            UpdateSoloCheckEvent()),"""
    ] 

    def __init__(self, parent, miner):
        threading.Thread.__init__(self)
        self.shutdown_event = threading.Event()
        self.parent = parent
        self.parent_name = parent.name
        self.miner = miner
        
    def run(self):
        logger.info(_('Listener for "%s" started') % self.parent_name)
        while not self.shutdown_event.is_set():
            line = self.miner.stdout.readline().strip()
            # logger.debug("Line: %s", line)
            if not line: continue
           
            for s, event_func in self.LINES: # Use self to allow subclassing
                match = re.search(s, line, flags=re.I)
                if match is not None:
                    event = event_func(match)
                    if event is not None:
                        wx.PostEvent(self.parent, event)
                    break     
           
            else:
                # Possible error or new message, just pipe it through
                event = UpdateStatusEvent(text=line)
                logger.info(_('Listener for "%(name)s": %(line)s'),
                            dict(name=self.parent_name, line=line))
                wx.PostEvent(self.parent, event)
        logger.info(_('Listener for "%s" shutting down'), self.parent_name)
    

class PhoenixListenerThread(MinerListenerThread):
    LINES = [
        (r"Result: .* accepted",
            lambda _: UpdateAcceptedEvent(accepted=True)),
        (r"Result: .* rejected", lambda _:
            UpdateAcceptedEvent(accepted=False)),
        (r"(\d+)\.?(\d*) Khash/sec", lambda match:
            UpdateHashRateEvent(rate=float(match.group(1) + '.' + match.group(2)))),
        (r"(\d+)\.?(\d*) Mhash/sec", lambda match:
            UpdateHashRateEvent(rate=float(match.group(1) + '.' + match.group(2)) * 1000)),
        (r"Currently on block",
            lambda _: None), # Just ignore lines like these
    ]
    
class CgListenerThread(MinerListenerThread):
    LINES = [
        (r" Accepted .*",
            lambda _: UpdateAcceptedEvent(accepted=True)),
        #(r"A:*\d+",
        #    lambda _: UpdateAcceptedEvent(accepted=False)),
        (r" Rejected .*",
            lambda _: UpdateAcceptedEvent(accepted=False)),
        #(r"R:*\d+",
        #    lambda _: UpdateAcceptedEvent(accepted=False)),
        #(r"Q:*\d+",
        #    lambda _: UpdateAcceptedEvent(accepted=False)),
        #(r"HW:*\d+",
        #    lambda _: UpdateAcceptedEvent(accepted=False)),
        #(r"\(\d+s\):(\d+)\.?(\d*) .* Kh/s", lambda match:
        #    UpdateHashRateEvent(rate=float(match.group(1) + '.' + match.group(2)) * 1000)),
        (r"\(*avg\):.*Kh", lambda match:
            UpdateHashRateEvent(rate=float(non_decimal.sub('', match.group(0))))),
        (r"\(*avg\):.*Mh", lambda match:
            UpdateHashRateEvent(rate=float(non_decimal.sub('', match.group(0))) * 1000)),
        (r"^GPU\s*\d+",
            lambda _: None), # Just ignore lines like these
    ]

# Below is kind of an ugly hack for updating reaper shares, but it works - TacoTime
class ReaperListenerThread(MinerListenerThread):
    LINES = [
        (r"GPU \d+.*", lambda match:
            ReaperAttributeUpdate(clstring=match.group(0)))
    ]
     
class CudaminerListenerThread(MinerListenerThread):
    LINES = [
        (r"(yay!!!)",
            lambda _: UpdateAcceptedEvent(accepted=True)),
        (r"(booooo)",
            lambda _: UpdateAcceptedEvent(accepted=False)),
        (r"\hashes, .*khash", lambda match:
            UpdateHashRateEvent(rate=float(non_decimal.sub('', match.group(0))))),
        #(r"^GPU\s*\d+",
        #    lambda _: None), # Just ignore lines like these
    ]
     
class ProxyListenerThread(MinerListenerThread):
    LINES = [
        (r".* accepted, .*",
            lambda _: UpdateAcceptedEvent(accepted=True)),
        (r".* REJECTED:.*",
            lambda _: UpdateAcceptedEvent(accepted=False)),
        (r".*LISTENING.*", lambda match:
            UpdateHashRateEvent(rate = -0.0000001)),
    ]

class MinerTab(wx.Panel):
    """A tab in the GUI representing a miner instance.

    Each MinerTab has these responsibilities:
    - Persist its data to and from the config file
    - Launch a backend subprocess and monitor its progress
      by creating a MinerListenerThread.
    - Post updates to the GUI's statusbar & summary panel; the format depends
      whether the backend is working solo or in a pool.
    """
    def __init__(self, parent, id, devices, servers, defaults, gpusettings_data, statusbar, data):
        wx.Panel.__init__(self, parent, id)
        self.parent = parent
        self.servers = servers
        self.defaults = defaults
        self.gpusettings_data = gpusettings_data
        self.statusbar = statusbar
        self.is_mining = False
        self.is_paused = False
        self.is_possible_error = False
        self.miner = None # subprocess.Popen instance when mining
        self.miner_listener = None # MinerListenerThread when mining
        self.solo_blocks_found = 0
        self.accepted_shares = 0 # shares for pool, diff1 hashes for solo
        self.accepted_times = collections.deque()
        self.invalid_shares = 0
        self.invalid_times = collections.deque()
        self.last_rate = 0 # units of khash/s
        self.autostart = False
        self.num_processors = int(os.getenv('NUMBER_OF_PROCESSORS', 1))
        self.affinity_mask = 0
        self.server_lbl = wx.StaticText(self, -1, _("Server:"))
        self.summary_panel = None # SummaryPanel instance if summary open
        self.server = wx.ComboBox(self, -1,
                                  choices=[s['name'] for s in servers],
                                  style=wx.CB_READONLY)
        self.gpusettings_lbl = wx.StaticText(self, -1, _("GPU Defaults:"))
        self.gpusettings = wx.ComboBox(self, -1,
                                  choices=[s['name'] for s in gpusettings_data],
                                  style=wx.CB_READONLY)
        self.website_lbl = wx.StaticText(self, -1, _("Website:"))
        self.website = hyperlink.HyperLinkCtrl(self, -1, "")
        self.external_lbl = wx.StaticText(self, -1, _("Ext. Path:"))
        self.txt_external = wx.TextCtrl(self, -1, "")
        self.host_lbl = wx.StaticText(self, -1, _("Host:"))
        self.txt_host = wx.TextCtrl(self, -1, "")
        self.port_lbl = wx.StaticText(self, -1, _("Port:"))
        self.txt_port = wx.TextCtrl(self, -1, "")
        self.user_lbl = wx.StaticText(self, -1, STR_USERNAME)
        self.txt_username = wx.TextCtrl(self, -1, "")
        self.pass_lbl = wx.StaticText(self, -1, STR_PASSWORD)
        self.txt_pass = wx.TextCtrl(self, -1, "", style=wx.TE_PASSWORD)
        self.device_lbl = wx.StaticText(self, -1, _("Device:"))
        self.device_listbox = wx.ComboBox(self, -1, choices=devices or [_("No OpenCL devices")], style=wx.CB_READONLY)
        self.minercgminer_lbl = wx.StaticText(self, -1, _("Miner: cgminer"))
        self.minerreaper_lbl = wx.StaticText(self, -1, _("Miner: reaper"))
        self.minercudaminer_lbl = wx.StaticText(self, -1, _("Miner: cudaminer"))
        self.proxy_lbl = wx.StaticText(self, -1, _("Stratum proxy"))
        self.thrcon_lbl = wx.StaticText(self, -1, _("Thread concurrency:"))
        self.txt_thrcon = wx.TextCtrl(self, -1, "")
        self.worksize_lbl = wx.StaticText(self, -1, _("Worksize:"))
        self.txt_worksize = wx.TextCtrl(self, -1, "")
        self.vectors_lbl = wx.StaticText(self, -1, _("Vectors:"))
        self.txt_vectors = wx.TextCtrl(self, -1, "")
        self.intensity_lbl = wx.StaticText(self, -1, _("Intensity:"))
        self.txt_intensity = wx.TextCtrl(self, -1, "")
        self.gputhreads_lbl = wx.StaticText(self, -1, _("GPU threads:"))
        self.txt_gputhreads = wx.TextCtrl(self, -1, "")
        self.flags_lbl = wx.StaticText(self, -1, _("Extra flags:"))
        self.txt_flags = wx.TextCtrl(self, -1, "")
        self.extra_info = wx.StaticText(self, -1, "")
        self.affinity_lbl = wx.StaticText(self, -1, _("CPU Affinity:"))
        #self.affinity_chks = [wx.CheckBox(self, label='%d ' % i)
        #                      for i in range(self.num_processors)]
        self.stratum_lbl = wx.StaticText(self, -1, _("Use stratum:"))
        self.txt_stratum = wx.ComboBox(self, -1,
                                  choices=['Yes','No'],
                                  style=wx.CB_READONLY)
        self.interactive_lbl = wx.StaticText(self, -1, _("Interactive:"))
        self.txt_interactive = wx.ComboBox(self, -1,
                                  choices=['Yes','No'],
                                  style=wx.CB_READONLY)
        self.texcache_lbl = wx.StaticText(self, -1, _("Texture cache:"))
        self.txt_texcache = wx.ComboBox(self, -1,
                                  choices=['Disabled','1D','2D'],
                                  style=wx.CB_READONLY)
        self.singlemem_lbl = wx.StaticText(self, -1, _("Multiblock memory:"))
        self.txt_singlemem = wx.ComboBox(self, -1,
                                  choices=['Yes','No'],
                                  style=wx.CB_READONLY)
        self.warpflags_lbl = wx.StaticText(self, -1, _("Warp configuration:"))
        self.txt_warpflags = wx.TextCtrl(self, -1, "")
        self.stratuminfo0_lbl = wx.StaticText(self, -1, _("Connect miners to"))
        self.stratuminfo1_lbl = wx.StaticText(self, -1, _("host: localhost port: 8332"))
        self.balance_lbl = wx.StaticText(self, -1, _("Balance:"))
        self.balance_amt = wx.StaticText(self, -1, "0")
        self.balance_refresh = wx.Button(self, -1, STR_REFRESH_BALANCE)
        self.balance_refresh_timer = wx.Timer()
        self.withdraw = wx.Button(self, -1, _("Withdraw"))
        self.balance_cooldown_seconds = 0
        self.balance_auth_token = ""

        self.labels = [self.minercgminer_lbl, self.minerreaper_lbl, 
                      self.proxy_lbl, self.server_lbl, 
                      self.website_lbl, self.host_lbl, 
                      self.port_lbl, self.user_lbl, 
                      self.pass_lbl, self.device_lbl, 
                      self.thrcon_lbl, self.vectors_lbl, 
                      self.intensity_lbl, self.gputhreads_lbl, 
                      self.worksize_lbl, self.stratum_lbl, 
                      self.stratuminfo0_lbl, self.stratuminfo1_lbl, 
                      self.flags_lbl, self.balance_lbl, 
                      self.interactive_lbl, self.texcache_lbl, 
                      self.singlemem_lbl, self.warpflags_lbl,
                      self.minercudaminer_lbl]
        self.txts = [self.txt_host, self.txt_port,
                     self.txt_username, self.txt_pass,
                     self.txt_thrcon, self.txt_worksize, 
                     self.txt_vectors, self.txt_intensity, 
                     self.txt_gputhreads, self.txt_stratum, 
                     self.txt_flags, self.txt_interactive, 
                     self.txt_texcache, self.txt_singlemem,
                     self.txt_warpflags]
        self.all_widgets = [self.server, 
                            self.website,
                            self.device_listbox,
                            self.balance_amt,
                            self.balance_refresh,
                            self.withdraw] + self.labels + self.txts
        self.hidden_widgets = [self.extra_info,
                               self.txt_external,
                               self.external_lbl]

        self.start = wx.Button(self, -1, STR_START_MINING)

        self.device_listbox.SetSelection(0)
        self.server.SetStringSelection(self.defaults.get('default_server'))

        self.set_data(data)

        for txt in self.txts:
            txt.Bind(wx.EVT_KEY_UP, self.check_if_modified)
        self.device_listbox.Bind(wx.EVT_COMBOBOX, self.check_if_modified)

        self.start.Bind(wx.EVT_BUTTON, self.toggle_mining)
        self.server.Bind(wx.EVT_COMBOBOX, self.on_select_server)
        self.gpusettings.Bind(wx.EVT_COMBOBOX, self.on_select_gpusettings)
        self.balance_refresh_timer.Bind(wx.EVT_TIMER, self.on_balance_cooldown_tick)
        self.balance_refresh.Bind(wx.EVT_BUTTON, self.on_balance_refresh)
        self.withdraw.Bind(wx.EVT_BUTTON, self.on_withdraw)
        #for chk in self.affinity_chks:
        #    chk.Bind(wx.EVT_CHECKBOX, self.on_affinity_check)
        self.Bind(EVT_UPDATE_HASHRATE, lambda event: self.update_khash(event.rate))
        self.Bind(EVT_UPDATE_ACCEPTED, lambda event: self.update_shares(event.accepted))
        self.Bind(EVT_REAPER_ATTRIBUTE_UPDATE, lambda event: self.update_attributes_reaper(event.clstring))
        self.Bind(EVT_UPDATE_REAPER_ACCEPTED, lambda event: self.update_shares_reaper(event.quantity, event.accepted))
        self.Bind(EVT_UPDATE_STATUS, lambda event: self.update_status(event.text))
        self.Bind(EVT_UPDATE_SOLOCHECK, lambda event: self.update_solo())
        self.update_statusbar()
        self.clear_summary_widgets()

    @property
    def last_update_time(self):
        """Return the local time of the last accepted share."""
        if self.accepted_times:
            return time.localtime(self.accepted_times[-1])
        return None

    @property
    def server_config(self):
        hostname = self.txt_host.GetValue()
        return self.get_server_by_field(hostname, 'host')
        
    @property
    def gpusettings_config(self):
        profilename = self.gpusettings_data.GetValue()
        return self.get_gpusettings_by_field(profilename, 'name')

    @property
    def is_solo(self):
        """Return True if this miner is configured for solo mining."""
        return self.server.GetStringSelection() == "solo"

    @property
    def is_modified(self):
        """Return True if this miner has unsaved changes pending."""
        return self.last_data != self.get_data()

    @property
    def external_path(self):
        """Return the path to an external miner, or "" if none is present."""
        return self.txt_external.GetValue()

    @property
    def is_external_miner(self):
        """Return True if this miner has an external path configured."""
        return self.txt_external.GetValue() != ""

    @property
    def host_with_http_prefix(self):
        """Return the host address, with http:// prepended if needed."""
        host = self.txt_host.GetValue()
        if not host.startswith("http://"):
            host = "http://" + host
        return host

    @property
    def host_without_http_prefix(self):
        """Return the host address, with http:// stripped off if needed."""
        host = self.txt_host.GetValue()
        if host.startswith("http://"):
            return host[len('http://'):]
        return host

    @property
    def device_index(self):
        """Return the index of the currently selected OpenCL device."""
        s = self.device_listbox.GetStringSelection()
        match = re.search(r'\[(\d+)-(\d+)\]', s)
        try: return int(match.group(2))
        except: return 0

    @property
    def platform_index(self):
        """Return the index of the currently selected OpenCL platform."""
        s = self.device_listbox.GetStringSelection()
        match = re.search(r'\[(\d+)-(\d+)\]', s)
        try: return int(match.group(1))
        except: return 0

    @property
    def is_device_visible(self):
        """Return True if we are using a backend with device selection."""
        NO_DEVICE_SELECTION = ['rpcminer', 'bitcoin-miner']
        return not any(d in self.external_path for d in NO_DEVICE_SELECTION)

    def on_affinity_check(self, event):
        """Set the affinity mask to the selected value."""
        self.affinity_mask = 0
        for i in range(self.num_processors):
            # is_checked = self.affinity_chks[i].GetValue()
            self.affinity_mask += (is_checked << i)
        if self.is_mining:
            try:
                set_process_affinity(self.miner.pid, self.affinity_mask)
            except:
                pass # TODO: test on Linux

    def pause(self):
        """Pause the miner if we are mining, otherwise do nothing."""
        if self.is_mining:
            self.stop_mining()
            self.is_paused = True

    def resume(self):
        """Resume the miner if we are paused, otherwise do nothing."""
        if self.is_paused:
            self.start_mining()
            self.is_paused = False

    def get_data(self):
        """Return a dict of our profile data."""
        return dict(name=self.name,
                    hostname=self.txt_host.GetValue(),
                    port=self.txt_port.GetValue(),
                    username=self.txt_username.GetValue(),
                    password=self.txt_pass.GetValue(),
                    device=self.device_listbox.GetSelection(),
                    flags=self.txt_flags.GetValue(),
                    thrcon=self.txt_thrcon.GetValue(),
                    worksize=self.txt_worksize.GetValue(),
                    vectors=self.txt_vectors.GetValue(),
                    intensity=self.txt_intensity.GetValue(),
                    gputhreads=self.txt_gputhreads.GetValue(),
                    stratum=self.txt_stratum.GetValue(),
                    autostart=self.autostart,
                    affinity_mask=self.affinity_mask,
                    balance_auth_token=self.balance_auth_token,
                    interactive=self.txt_interactive.GetValue(),
                    texcache=self.txt_texcache.GetValue(),
                    singlemem=self.txt_singlemem.GetValue(),
                    warpflags=self.txt_warpflags.GetValue(),
                    external_path=self.external_path)

    def set_data(self, data):
        """Set our profile data to the information in data. See get_data()."""
        self.last_data = data
        default_server_config = self.get_server_by_field(
                                    self.defaults['default_server'], 'name')
        self.name = (data.get('name') or _('Default'))

        # Backwards compatibility: hostname key used to be called server.
        # We only save out hostname now but accept server from old INI files.
        hostname = (data.get('hostname') or _('')) # Hack by tacotime, don't give it any host, the user can enter it
        external_path_ref = (data.get('external_path') or _('CGMINER')) # Default miner is cgminer
        
        self.txt_host.SetValue(hostname)
        self.txt_external.SetValue(external_path_ref)
        self.txt_thrcon.SetValue(data.get('thrcon') or _(''))
        self.txt_worksize.SetValue(data.get('worksize') or _(''))
        self.txt_vectors.SetValue(data.get('vectors') or _(''))
        self.txt_intensity.SetValue(data.get('intensity') or _(''))
        self.txt_gputhreads.SetValue(data.get('gputhreads') or _(''))
        self.txt_stratum.SetValue(data.get('stratum') or _('Yes'))
        self.txt_interactive.SetValue(data.get('interactive') or _('Yes'))
        self.txt_texcache.SetValue(data.get('texcache') or _('Disabled'))
        self.txt_singlemem.SetValue(data.get('singlemem') or _('Yes'))
        self.txt_warpflags.SetValue(data.get('warpflags') or _('auto'))
        
        self.server.SetStringSelection(self.server_config.get('name', "Other"))

        self.txt_username.SetValue(
            data.get('username') or
            self.defaults.get('default_username', ''))

        self.txt_pass.SetValue(
            data.get('password') or
            self.defaults.get('default_password', ''))

        self.txt_port.SetValue(str(
            data.get('port') or
            self.server_config.get('port', 3333)))

        self.txt_flags.SetValue(data.get('flags', ''))
        self.autostart = data.get('autostart', False)
        self.affinity_mask = data.get('affinity_mask', 1)
        for i in range(self.num_processors):
            # self.affinity_chks[i].SetValue((self.affinity_mask >> i) & 1)
            pass

        # Handle case where they removed devices since last run.
        device_index = data.get('device', None)
        if device_index is not None and device_index < self.device_listbox.GetCount():
            self.device_listbox.SetSelection(device_index)

        # self.change_gpusettings(self.gpusettings_config)
        self.change_server(self.server_config)

        self.balance_auth_token = data.get('balance_auth_token', '')

    def clear_summary_widgets(self):
        """Release all our summary widgets."""
        self.summary_name = None
        self.summary_status = None
        self.summary_shares_accepted = None
        self.summary_shares_stale = None
        self.summary_start = None
        self.summary_autostart = None

    def get_start_stop_state(self):
        """Return appropriate text for the start/stop button."""
        return _("Stop") if self.is_mining else _("Start")

    def get_start_label(self):
        return STR_STOP_MINING if self.is_mining else STR_START_MINING

    def update_summary(self):
        """Update our summary fields if possible."""
        if not self.summary_panel:
            return

        self.summary_name.SetLabel(self.name)
        if self.is_paused:
            text = STR_PAUSED
        elif not self.is_mining:
            text = STR_STOPPED
        elif self.is_possible_error:
            text = _("Connection problems")
        elif (self.last_rate == -0.0000001):
            text = _("Proxy connected")
        else:
            text = format_khash(self.last_rate)
            
        self.summary_status.SetLabel(text)
        
        # Original
        # self.summary_shares_accepted.SetLabel("%d (%d)" % 
        #     (self.accepted_shares, len(self.accepted_times)))
        # New - Don't care about accepted_times since reaper doesn't have them - TacoTime
        self.summary_shares_accepted.SetLabel("%d" % 
            (self.accepted_shares))

        # Original
        # if self.is_solo:
        #    self.summary_shares_invalid.SetLabel("-")
        # else:
        #     self.summary_shares_invalid.SetLabel("%d (%d)" % 
        #         (self.invalid_shares, len(self.invalid_times)))
        # New - Don't care about invalid_times since reaper doesn't have them - TacoTime
        if self.is_solo:
            self.summary_shares_invalid.SetLabel("-")
        else:
            self.summary_shares_invalid.SetLabel("%d" % 
                (self.invalid_shares))

        self.summary_start.SetLabel(self.get_start_stop_state())
        self.summary_autostart.SetValue(self.autostart)
        self.summary_panel.grid.Layout()

    def get_summary_widgets(self, summary_panel):
        """Return a list of summary widgets suitable for sizer.AddMany."""
        self.summary_panel = summary_panel
        self.summary_name = wx.StaticText(summary_panel, -1, self.name)
        self.summary_name.Bind(wx.EVT_LEFT_UP, self.show_this_panel)

        self.summary_status = wx.StaticText(summary_panel, -1, STR_STOPPED)
        self.summary_shares_accepted = wx.StaticText(summary_panel, -1, "0")
        self.summary_shares_invalid = wx.StaticText(summary_panel, -1, "0")
        self.summary_start = wx.Button(summary_panel, -1, self.get_start_stop_state(), style=wx.BU_EXACTFIT)
        self.summary_start.Bind(wx.EVT_BUTTON, self.toggle_mining)
        self.summary_autostart = wx.CheckBox(summary_panel, -1)
        self.summary_autostart.Bind(wx.EVT_CHECKBOX, self.toggle_autostart)
        self.summary_autostart.SetValue(self.autostart)
        return [
            (self.summary_name, 0, wx.ALIGN_CENTER_HORIZONTAL),
            (self.summary_status, 0, wx.ALIGN_CENTER_HORIZONTAL, 0),
            (self.summary_shares_accepted, 0, wx.ALIGN_CENTER_HORIZONTAL, 0),
            (self.summary_shares_invalid, 0, wx.ALIGN_CENTER_HORIZONTAL, 0),
            (self.summary_start, 0, wx.ALIGN_CENTER, 0),
            (self.summary_autostart, 0, wx.ALIGN_CENTER, 0)
        ]

    def show_this_panel(self, event):
        """Set focus to this panel."""
        self.parent.SetSelection(self.parent.GetPageIndex(self))

    def toggle_autostart(self, event):
        self.autostart = event.IsChecked()

    def toggle_mining(self, event):
        """Stop or start the miner."""
        if self.is_mining:
            self.stop_mining()
        else:
            self.start_mining()
        self.update_summary()

    #############################
    # Begin backend specific code
    def configure_subprocess_poclbm(self):
        """Set up the command line for poclbm."""
        folder = get_module_path()
        if USE_MOCK:
            executable = "python mockBitcoinMiner.py"
        else:
            if hasattr(sys, 'frozen'):
                executable = "poclbm.exe"
            else:
                executable = "python poclbm.py"
        cmd = "%s %s:%s@%s:%s --device=%d --platform=%d --verbose -r1 %s" % (
                executable,
                self.txt_username.GetValue(),
                self.txt_pass.GetValue(),
                self.txt_host.GetValue(),
                self.txt_port.GetValue(),
                self.device_index,
                self.platform_index,
                self.txt_flags.GetValue()
        )
        return cmd, folder

    def configure_subprocess_rpcminer(self):
        """Set up the command line for rpcminer.

        The hostname must start with http:// for these miners.
        """
        cmd = "%s -user=%s -password=%s -url=%s:%s %s" % (
            self.external_path,
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            self.host_with_http_prefix,
            self.txt_port.GetValue(),
            self.txt_flags.GetValue()
        )
        return cmd, os.path.dirname(self.external_path)

    def configure_subprocess_ufasoft(self):
        """Set up the command line for ufasoft's SSE2 miner.

        The hostname must start with http:// for these miners.
        """
        cmd = "%s -u %s -p %s -o %s:%s %s" % (
            self.external_path,
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            self.host_with_http_prefix,
            self.txt_port.GetValue(),
            self.txt_flags.GetValue())
        return cmd, os.path.dirname(self.external_path)

    def configure_subprocess_phoenix(self):
        """Set up the command line for phoenix miner."""
        path = self.external_path
        if path.endswith('.py'):
            path = "python " + path

        cmd = "%s -u http://%s:%s@%s:%s PLATFORM=%d DEVICE=%d %s" % (
            path,
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            self.host_without_http_prefix,
            self.txt_port.GetValue(),
            self.platform_index,
            self.device_index,
            self.txt_flags.GetValue())
        return cmd, os.path.dirname(self.external_path)

    def configure_subprocess_cgminer(self):
        """Set up the command line for cgminer."""

        # Set the path for cgminer, should be ./cgminer/cgminer.exe
        # Not set up for unix, modify this to /cgminer/cgminer for unix
        os.chdir(STARTUP_PATH)
        path = '\"' + STARTUP_PATH + "\\cgminer\\cgminer.exe" + '\"'
        cgdir = STARTUP_PATH + "\\cgminer\\"
        
        #if path.endswith('.py'):
        #    path = "python " + path
        
        if self.txt_stratum.GetValue() == "Yes":
            http_header = "stratum+tcp://"
        else:
            http_header = "http://"

        # Command line arguments for cgminer here:
        # -u <username>
        # -p <password>
        # -o <http://server.ip:port>
        # --gpu-platform <like it sounds>
        # -w <worksize>
        # -v <vectors>
        # -d <device appear in pyopencl>
        # -l <log message period in second>
        # -T <disable curses interface and output to console (stdout)>
        # -g <GPU threads>
        cmd = "%s --scrypt -u %s -p %s -o %s%s:%s --gpu-platform %s -d %s -w %s -v %s -I %s -g %s -l 1 -T %s --thread-concurrency %s" % (
            path,
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            http_header,
            self.host_without_http_prefix,
            self.txt_port.GetValue(),
            self.platform_index,
            self.device_index,
            self.txt_worksize.GetValue(),
            self.txt_vectors.GetValue(),
            self.txt_intensity.GetValue(),
            self.txt_gputhreads.GetValue(),
            self.txt_flags.GetValue(),
            self.txt_thrcon.GetValue())
        
        # Full console command for batch file creation given below; don't add -T in this instance so end user gets full output
        full_console_cmd = "%s --scrypt -u %s -p %s -o %s%s:%s --gpu-platform %s -d %s -w %s -v %s -I %s -g %s -l 1 %s --thread-concurrency %s" % (
            path,
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            http_header,
            self.host_without_http_prefix,
            self.txt_port.GetValue(),
            self.platform_index,
            self.device_index,
            self.txt_worksize.GetValue(),
            self.txt_vectors.GetValue(),
            self.txt_intensity.GetValue(),
            self.txt_gputhreads.GetValue(),
            self.txt_flags.GetValue(),
            self.txt_thrcon.GetValue())
            
        f = open(cgdir + "mine-" +  self.name + ".bat", 'w')
        f.write(full_console_cmd)
        f.close()
        
        return cmd, os.path.dirname(path)

    def write_reaper_configs(self, reaperdir):
        # reaper.conf
        f = open(reaperdir + "\\reaper.conf", 'w')
        f.write("kernel reaper.cl\n")
        f.write("save_binaries yes\n")
        f.write("enable_graceful_shutdown no\n")
        f.write("long_polling yes\n")
        f.write("platform " + str(self.platform_index) + "\n")
        f.write("device " + str(self.device_index) + "\n\n")
        f.write("mine litecoin\n")
        f.close()
        
        # litecoin.conf
        f = open(reaperdir + "\\litecoin.conf", 'w')
        f.write("host " + self.host_without_http_prefix + "\n")
        f.write("port " + self.txt_port.GetValue() + "\n")
        f.write("user " + self.txt_username.GetValue() + "\n")
        f.write("pass " + self.txt_pass.GetValue() + "\n\n")
        f.write("protocol litecoin\n\n")
        f.write("gpu_thread_concurrency " + self.txt_thrcon.GetValue() + "\n")
        f.write("worksize " + self.txt_worksize.GetValue() + "\n")
        f.write("vectors " + self.txt_vectors.GetValue() + "\n")
        f.write("aggression " + self.txt_intensity.GetValue() + "\n")
        f.write("threads_per_gpu " + self.txt_gputhreads.GetValue() + "\n")
        f.write("sharethreads 32\n")
        f.write("lookup_gap 2\n")
        f.close()
        
    def configure_subprocess_reaper(self):
        """Set up the command line for reaper."""
        os.chdir(STARTUP_PATH)
        
        if os.path.exists(STARTUP_PATH + "\\reaper"):
            if os.path.exists(STARTUP_PATH + "\\reaper-" + self.name):
                logger.info("Reaper folder for miner already exists, writing config and commencing with mining.")
                self.write_reaper_configs(STARTUP_PATH + "\\reaper-" + self.name)
            else:
                logger.info("Reaper folder for miner missing, adding folder, files, and config.")
                os.makedirs(STARTUP_PATH + "\\reaper-" + self.name)
                distutils.dir_util.copy_tree(STARTUP_PATH + "\\reaper", os.getcwd() + "\\reaper-" + self.name)
                self.write_reaper_configs(STARTUP_PATH + "\\reaper-" + self.name)
        else:
            logger.info("Reaper folder with binaries is missing; can not mine!  Add reaper to ./reaper/ folder please.")

        path = STARTUP_PATH + "\\reaper-" + self.name

        # Have to change working directory, windows pain in the ass for reaper - TacoTime
        os.chdir(path)
        cmd =  '\"' + path + "\\reaper.exe" +  '\"' # Change this for unix!!!  - TacoTime
        return cmd, os.path.dirname(path)      
        
    def configure_subprocess_cudaminer(self):
        os.chdir(STARTUP_PATH)
        path =  '\"' + STARTUP_PATH + "\\cudaminer\\cudaminer.exe" + '\"' # Change this for unix!!!  - TacoTime
        
        os.chdir(STARTUP_PATH)
        cudaminerpath = STARTUP_PATH + "\\cudaminer\\"
        
        flag_interactive = 0 # Flag for cudaminer interactive setting
        if (self.txt_interactive.GetValue() == "Yes"):
            flag_interactive = 1
        else:
            flag_interactive = 0

        flag_texcache = 1
        if (self.txt_texcache.GetValue() == "Disabled"):
            flag_texcache = 0
        elif (self.txt_texcache.GetValue() == "1D"):
            flag_texcache = 1
        else:
            flag_texcache = 2
            
        flag_singlemem = 1
        if (self.txt_texcache.GetValue() == "Yes"):
            flag_singlemem = 0
        else:
            flag_singlemem = 1
            
        # Command line arguments for cudaminer here:
        # -o host and port prefixed with http://
        # -O username:password
        # -d device number (CUDA platform)
        # -i interactive (bool)
        # -l kernel/warp configuration (string len 4 or 5?)
        # -C Texture cache (0=disabled, 1=1D, 2=2D)
        # -m Single memory block (bool)
        cmd = "%s -o http://%s:%s/ -O %s:%s -d %s -i %s -l %s -C %s -m %s" % (
            path,
            self.host_without_http_prefix,
            self.txt_port.GetValue(),
            self.txt_username.GetValue(),
            self.txt_pass.GetValue(),
            self.device_index,
            flag_interactive,
            self.txt_warpflags.GetValue(),
            flag_texcache,
            flag_singlemem)
            
        # Create a batch file in case the user wants to try it out in console, too 
        f = open(cudaminerpath + "mine-" +  self.name + ".bat", 'w')
        f.write(cmd)
        f.close()
        
        return cmd, os.path.dirname(path)   
        
    def configure_subprocess_stratumproxy(self):
        """Set up the command line for proxy miner."""
        os.chdir(STARTUP_PATH)
        path = STARTUP_PATH + "\\stratumproxy\\mining_proxy.exe"
        if path.endswith('.py'):
            path = "python " + path

        # Command line arguments for cgminer here:
        # -u <username>
        # -p <password>
        # -o <http://server.ip:port>
        # -d <device appear in pyopencl>
        # -l <log message period in second>
        # -T <disable curses interface and output to console (stdout)>
        cmd = "%s -pa scrypt -o %s -p %s %s" % (
            path,
            self.host_without_http_prefix,
            self.txt_port.GetValue(),
            self.txt_flags.GetValue())
        return cmd, os.path.dirname(path)

    # End backend specific code
    ###########################

    def start_mining(self):
        """Launch a miner subprocess and attach a MinerListenerThread."""
        self.is_paused = False

        # Avoid showing a console window when frozen
        try: import win32process
        except ImportError: flags = 0
        else: flags = win32process.CREATE_NO_WINDOW

        # Determine what command line arguments to use

        listener_cls = MinerListenerThread
        
        if not self.is_external_miner:
            conf_func = self.configure_subprocess_poclbm
        elif "rpcminer" in self.external_path:
            conf_func = self.configure_subprocess_rpcminer
        elif "bitcoin-miner" in self.external_path:
            conf_func = self.configure_subprocess_ufasoft
        elif "phoenix" in self.external_path:
            conf_func = self.configure_subprocess_phoenix
            listener_cls = PhoenixListenerThread
        elif "CGMINER" in self.external_path:
            conf_func = self.configure_subprocess_cgminer
            listener_cls = CgListenerThread
        elif "REAPER" in self.external_path:
            conf_func = self.configure_subprocess_reaper
            listener_cls = ReaperListenerThread
        elif "CUDAMINER" in self.external_path:
            conf_func = self.configure_subprocess_cudaminer
            listener_cls = CudaminerListenerThread
        elif "PROXY" in self.external_path:
            conf_func = self.configure_subprocess_stratumproxy
            listener_cls = ProxyListenerThread
        else:
            raise ValueError # TODO: handle unrecognized miner
        cmd, cwd = conf_func()

        # for ufasoft:
        #  redirect stderr to stdout
        #  use universal_newlines to catch the \r output on Mhash/s lines
        try:
            logger.debug(_('Running command: ') + cmd)
            # for cgminer: 
            if conf_func == self.configure_subprocess_cgminer:
                cgminer_env = os.environ # Create an environment to set below environmental variable in
                cgminer_env['GPU_MAX_ALLOC_PERCENT'] = "100" # Set this environmental variable so we can use high thread concurrencies in cgminer
                self.miner = subprocess.Popen(cmd,
                                              env=cgminer_env,
                                              stdout=subprocess.PIPE,
                                              stderr=None,
                                              universal_newlines=True,
                                              creationflags=0x08000000,
                                              shell=(sys.platform != 'win32'))
            else:
                self.miner = subprocess.Popen(cmd,
                                              stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT,
                                              universal_newlines=True,
                                              creationflags=0x08000000,
                                              shell=(sys.platform != 'win32'))
            
        except OSError:
            raise #TODO: the folder or exe could not exist
        self.miner_listener = listener_cls(self, self.miner)
        self.miner_listener.daemon = True
        self.miner_listener.start()
        self.is_mining = True
        self.set_status(STR_STARTING, 1)
        self.start.SetLabel(self.get_start_label())

        try:
            set_process_affinity(self.miner.pid, self.affinity_mask)
        except:
            pass # TODO: test on Linux

    def on_close(self):
        """Prepare to close gracefully."""
        self.stop_mining()
        self.balance_refresh_timer.Stop()

    def stop_mining(self):
        """Terminate the poclbm process if able and its associated listener."""
        if self.miner is not None:
            if self.miner.returncode is None:
                # It didn't return yet so it's still running.
                try:
                    self.miner.terminate()
                except OSError:
                    pass # TODO: Guess it wasn't still running?
            self.miner = None
        if self.miner_listener is not None:
            self.miner_listener.shutdown_event.set()
            self.miner_listener = None
        self.is_mining = False
        self.is_paused = False
        self.set_status(STR_STOPPED, 1)
        self.start.SetLabel(self.get_start_label())

    def update_khash(self, rate):
        """Update our rate according to a report from the listener thread.

        If we are receiving rate messages then it means poclbm is no longer
        reporting errors.
        """
        self.last_rate = rate
        self.set_status(format_khash(rate), 1)
        
        if self.is_possible_error:
            self.update_statusbar()
            self.is_possible_error = False
            
    def update_shares_reaper(self, quantity, accepted):
        if self.is_solo:
            self.solo_blocks_found = quantity
        elif accepted:
            self.accepted_shares = quantity
        else:
            self.invalid_shares = quantity
        self.update_last_time(accepted) # BUG: This doesn't work right, but let's ignore it for now - TacoTime
        self.update_statusbar()
        
    def update_attributes_reaper(self, clstring):
        sharesA = int(non_decimal.sub('', re.search(r"shares: *\d+\|", clstring).group(0)))
        sharesR = int(non_decimal.sub('', re.search(r"\|\d+,", clstring).group(0)))
        hashrate = float(non_decimal.sub('', re.search(r"\~.*kH", clstring).group(0)))
        
        if self.is_solo:
            self.solo_blocks_found = sharesA
        else:
            self.accepted_shares = sharesA
        
        self.invalid_shares = sharesR
        self.last_rate = hashrate
        self.set_status(format_khash(hashrate), 1)
        if self.is_possible_error:
            self.update_statusbar()
            self.is_possible_error = False
        self.update_statusbar()

    def update_statusbar(self):
        """Show the shares or equivalent on the statusbar."""
        if self.is_solo:
            text = _("Difficulty 1 hashes: %(nhashes)d %(update_time)s") % \
                dict(nhashes=self.accepted_shares,
                     update_time=self.format_last_update_time())
            if self.solo_blocks_found > 0:
                block_text = _("Blocks: %d, ") % self.solo_blocks_found
                text = block_text + text
        else:
            text = _("Shares: %d accepted") % self.accepted_shares
            if self.invalid_shares > 0:
                text += _(", %d stale/invalid") % self.invalid_shares
            text += " %s" % self.format_last_update_time()
        self.set_status(text, 0)

    def update_last_time(self, accepted):
        """Set the last update time to now (in local time)."""

        now = time.time()
        if accepted:
            self.accepted_times.append(now)
            while now - self.accepted_times[0] > SAMPLE_TIME_SECS:
                self.accepted_times.popleft()
        else:
            self.invalid_times.append(now)
            while now - self.invalid_times[0] > SAMPLE_TIME_SECS:
                self.invalid_times.popleft()

    def format_last_update_time(self):
        """Format last update time for display."""
        time_fmt = '%I:%M:%S%p'
        if self.last_update_time is None:
            return ""
        return _("- last at %s") % time.strftime(time_fmt, self.last_update_time)

    def update_shares(self, accepted):
        """Update our shares with a report from the listener thread."""
        if self.is_solo and accepted:
            self.solo_blocks_found += 1
        elif accepted:
            self.accepted_shares += 1
        else:
            self.invalid_shares += 1
        self.update_last_time(accepted)
        self.update_statusbar()

    def update_status(self, msg):
        """Update our status with a report from the listener thread.

        If we receive a message from poclbm we don't know how to interpret,
        it's probably some kind of error state - in this case the best
        thing to do is just show it to the user on the status bar.
        """
        self.set_status(msg)
        if self.last_rate == -0.0000001:
            self.is_possible_error = False
        else:
            self.is_possible_error = True

    def set_status(self, msg, index=0):
        """Set the current statusbar text, but only if we have focus."""
        if self.parent.GetSelection() == self.parent.GetPageIndex(self):
            self.statusbar.SetStatusText(msg, index)

    def on_focus(self):
        """When we receive focus, update our status.

        This ensures that when switching tabs, the statusbar always
        shows the current tab's status.
        """
        self.update_statusbar()
        if self.is_mining:
            self.update_khash(self.last_rate)
        else:
            self.set_status(STR_STOPPED, 1)

    def get_taskbar_text(self):
        """Return text for the hover state of the taskbar."""
        rate = format_khash(self.last_rate) if self.is_mining else STR_STOPPED
        return "%s: %s" % (self.name, rate)

    def update_solo(self):
        """Update our easy hashes with a report from the listener thread."""
        self.accepted_shares += 1
        self.update_last_time(True)
        self.update_statusbar()
        
    def on_select_gpusettings(self, event):
        """Update our info in response to a new server choice."""
        new_gpusettings_name = str(self.gpusettings.GetValue())
        new_gpusettings = self.get_gpusettings_by_field(new_gpusettings_name, 'name')
        self.change_gpusettings(new_gpusettings)

    def on_select_server(self, event):
        """Update our info in response to a new server choice."""
        print self.server.GetValue()
        new_server_name = self.server.GetValue()
        new_server = self.get_server_by_field(new_server_name, 'name')
        self.change_server(new_server)

    def get_gpusettings_by_field(self, target_val, field):
        """Return the first server dict with the specified val, or {}."""
        for s in self.gpusettings_data:
            if s.get(field) == target_val:
                return s
        return {}        
        
    def get_server_by_field(self, target_val, field):
        """Return the first server dict with the specified val, or {}."""
        for s in self.servers:
            if s.get(field) == target_val:
                return s
        return {}

    def set_widgets_visible(self, widgets, show=False):
        """Show or hide each widget in widgets according to the show flag."""
        for w in widgets:
            if show:
                w.Show()
            else:
                w.Hide()

    def set_tooltips(self):
        add_tooltip(self.server, _("Server to connect to. Different servers have different fees and features.\nCheck their websites for full information."))
        add_tooltip(self.website, _("Website of the currently selected server. Click to visit."))
        add_tooltip(self.device_listbox, _("Available OpenCL devices on your system."))
        add_tooltip(self.txt_host, _("Host address, without http:// prefix."))
        add_tooltip(self.txt_port, _("Server port. This is usually 8332 for getwork or 3333 for stratum."))
        add_tooltip(self.txt_username, _("The miner's username.\nMay be different than your account username.\nExample: Kiv.GPU"))
        add_tooltip(self.txt_pass, _("The miner's password.\nMay be different than your account password."))
        add_tooltip(self.txt_flags, _("Extra flags to pass to the miner."))
        add_tooltip(self.txt_thrcon, _("Set the memory size for the scrypt kernel to use.\n1 unit = 64 KB"))
        add_tooltip(self.txt_worksize, _("Set the worksize value.\nDefault: 256"))
        add_tooltip(self.txt_vectors, _("Set the vectors value.\nDefault: 1"))
        add_tooltip(self.txt_gputhreads, _("Set the number of default threads to use.\nDefault: 1"))
        add_tooltip(self.txt_intensity, _("Set the intensity/aggression value.\nHigh intensity: 18-20\nLow intensity: 10-14"))
        add_tooltip(self.gpusettings, _("Default values for any given AMD video card.\nTry these first if you are new to scrypt mining."))
        add_tooltip(self.txt_interactive, _("Run in interactive mode so that the desktop remains usable while mining.\nMay slow hash rate."))
        add_tooltip(self.txt_texcache, _("Enable use of 1D or 2D texture cache for mining."))
        add_tooltip(self.txt_singlemem, _("Use multiple blocks of memory or a single block of memory for mining."))
        add_tooltip(self.txt_warpflags, _("String in S##x# or ##x# format that gives the warp configuration.\nExamples: S27x3 or 28x4.\nUse auto for automatic warp configuration tuning."))
        #for chk in self.affinity_chks:
        #    add_tooltip(chk, _("CPU cores used for mining.\nUnchecking some cores can reduce high CPU usage in some systems."))

    def reset_statistics(self):
        """Reset our share statistics to zero."""
        self.solo_blocks_found = 0
        self.accepted_shares = 0
        self.accepted_times.clear()
        self.invalid_shares = 0
        self.invalid_times.clear()
        self.update_statusbar()
        
    def change_gpusettings(self, new_gpusettings):
        self.reset_statistics()
        if 'thread_concurrency' in new_gpusettings:
            self.txt_thrcon.SetValue(str(new_gpusettings['thread_concurrency']))
        if 'worksize' in new_gpusettings:
            self.txt_worksize.SetValue(str(new_gpusettings['worksize']))
        if 'vectors' in new_gpusettings:
            self.txt_vectors.SetValue(str(new_gpusettings['vectors']))
        if 'gputhreads' in new_gpusettings:
            self.txt_gputhreads.SetValue(str(new_gpusettings['gputhreads']))
        if 'intensity' in new_gpusettings:
            self.txt_intensity.SetValue(str(new_gpusettings['intensity']))

    def change_server(self, new_server):
        """Change the server to new_server, updating fields as needed."""
        self.reset_statistics()

        # Set defaults before we do server specific code
        self.set_tooltips()
        self.set_widgets_visible(self.all_widgets, True)
        self.withdraw.Disable()

        url = new_server.get('url', 'n/a')
        self.website.SetLabel(url)
        self.website.SetURL(url)

        # Invalidate any previous auth token since it won't be valid for the
        # new server.
        self.balance_auth_token = ""

        if 'host' in new_server:
            self.txt_host.SetValue(new_server['host'])
        if 'port' in new_server:
            self.txt_port.SetValue(str(new_server['port']))


        # Call server specific code.
        host = new_server.get('host', "").lower()
        if host == "api2.bitcoin.cz" or host == "mtred.com": self.layout_slush()
        if "eligius.st" in host: self.layout_eligius()
        elif host == "bitpenny.dyndns.biz": self.layout_bitpenny()
        elif host == "pit.deepbit.net": self.layout_deepbit()
        elif host == "btcmine.com": self.layout_btcmine()
        elif host == "rr.btcmp.com": self.layout_btcmp()
        elif "btcguild.com" in host: self.layout_btcguild()
        elif host == "bitcoin-server.de": self.layout_bitcoinserver
        elif host == "pit.x8s.de": self.layout_x8s()
        elif self.external_path == "CUDAMINER": self.layout_cudaminer()
        else: self.layout_default()

        self.Layout()

        self.update_tab_name()

    def on_balance_cooldown_tick(self, event=None):
        """Each second, decrement the cooldown for refreshing balance."""
        self.balance_cooldown_seconds -= 1
        self.balance_refresh.SetLabel("%d..." % self.balance_cooldown_seconds)
        if self.balance_cooldown_seconds <= 0:
            self.balance_refresh_timer.Stop()
            self.balance_refresh.Enable()
            self.balance_refresh.SetLabel(STR_REFRESH_BALANCE)

    def require_auth_token(self):
        """Prompt the user for an auth token if they don't have one already.

        Set the result to self.balance_auth_token and return None.
        """
        if self.balance_auth_token:
            return
        url = self.server_config.get('balance_token_url')
        dialog = BalanceAuthRequest(self, url)
        dialog.txt_token.SetFocus()
        result = dialog.ShowModal()
        dialog.Destroy()
        if result == wx.ID_CANCEL:
            return
        self.balance_auth_token = dialog.get_value() # TODO: validate token?

    def is_auth_token_rejected(self, response):
        """If the server rejected our token, reset auth_token and return True.

        Otherwise, return False.
        """
        if response.status in [401, 403]: # 401 Unauthorized or 403 Forbidden
            # Token rejected by the server - reset their token so they'll be
            # prompted again
            self.balance_auth_token = ""
            return True
        return False

    def request_balance_get(self, balance_auth_token, use_https=False):
        """Request our balance from the server via HTTP GET and auth token.

        This method should be run in its own thread.
        """
        response, data = http_request(
            self.server_config['balance_host'],
            "GET",
            self.server_config["balance_url"] % balance_auth_token,
            use_https=use_https
        )
        if self.is_auth_token_rejected(response):
            data = _("Auth token rejected by server.")
        elif not data:
            data = STR_CONNECTION_ERROR
        else:
            try:
                info = json.loads(data)
                confirmed = (info.get('confirmed_reward') or
                             info.get('confirmed') or
                             info.get('balance') or
                             info.get('user', {}).get('confirmed_rewards') or
                             0)
                unconfirmed = (info.get('unconfirmed_reward') or
                               info.get('unconfirmed') or
                               info.get('user', {}).get('unconfirmed_rewards') or
                               0)
                if self.server_config.get('host') == "pit.deepbit.net":
                    ipa = info.get('ipa', False)
                    self.withdraw.Enable(ipa)

                if self.server_config.get('host') == "rr.btcmp.com":
                    ipa = info.get('can_payout', False)
                    self.withdraw.Enable(ipa)

                data = _("%s confirmed") % format_balance(confirmed)
                if unconfirmed > 0:
                    data += _(", %s unconfirmed") % format_balance(unconfirmed)
            except: # TODO: what exception here?
                data = _("Bad response from server.")

        wx.CallAfter(self.balance_amt.SetLabel, data)

    def on_withdraw(self, event):
        self.withdraw.Disable()
        host = self.server_config.get('host')
        if host == 'bitpenny.dyndns.biz':
            self.withdraw_bitpenny()
        elif host == 'pit.deepbit.net':
            self.withdraw_deepbit()
        elif host == 'rr.btcmp.com':
            self.withdraw_btcmp()

    def requires_auth_token(self, host):
        """Return True if the specified host requires an auth token for balance update."""
        HOSTS_REQUIRING_AUTH_TOKEN = ["api2.bitcoin.cz",
                                      "btcmine.com",
                                      "pit.deepbit.net",
                                      "pit.x8s.de",
                                      "mtred.com",
                                      "rr.btcmp.com",
                                      "bitcoin-server.de"]
        if host in HOSTS_REQUIRING_AUTH_TOKEN: return True        
        if "btcguild" in host: return True      
        return False
    
    def requires_https(self, host):
        """Return True if the specified host requires HTTPs for balance update."""
        HOSTS = ["mtred.com", "api2.bitcoin.cz"]
        if host in HOSTS: return True
        if "btcguild" in host: return True
        return False
    
    def on_balance_refresh(self, event=None):
        """Refresh the miner's balance from the server."""
        host = self.server_config.get("host")
        if self.requires_auth_token(host):
            self.require_auth_token()
            if not self.balance_auth_token: # They cancelled the dialog
                return
            try:
                self.balance_auth_token.decode('ascii')
            except UnicodeDecodeError:
                return # Invalid characters in auth token
            self.http_thread = threading.Thread(
                target=self.request_balance_get,
                args=(self.balance_auth_token,),
                kwargs=dict(use_https=self.requires_https(host)))
            self.http_thread.start()
        elif host == 'bitpenny.dyndns.biz':
            self.http_thread = threading.Thread(
            target=self.request_payout_bitpenny, args=(False,))
            self.http_thread.start()
        elif 'eligius.st' in host:
            self.http_thread = threading.Thread(
                target=self.request_balance_eligius
            )
            self.http_thread.start()

        self.balance_refresh.Disable()
        self.balance_cooldown_seconds = 10
        self.balance_refresh_timer.Start(1000)

    #################################
    # Begin server specific HTTP code

    def withdraw_btcmp(self):
        """Launch a thread to withdraw from deepbit."""
        self.require_auth_token()
        if not self.balance_auth_token: # User refused to provide token
            return
        self.http_thread = threading.Thread(
                target=self.request_payout_btcmp,
                args=(self.balance_auth_token,))
        self.http_thread.start()

    def withdraw_deepbit(self):
        """Launch a thread to withdraw from deepbit."""
        self.require_auth_token()
        if not self.balance_auth_token: # User refused to provide token
            return
        self.http_thread = threading.Thread(
                target=self.request_payout_deepbit,
                args=(self.balance_auth_token,))
        self.http_thread.start()

    def withdraw_bitpenny(self):
        self.http_thread = threading.Thread(
            target=self.request_payout_bitpenny, args=(True,))
        self.http_thread.start() # TODO: look at aliasing of this variable

    def request_payout_btcmp(self, balance_auth_token):
        """Request payout from btcmp's server via HTTP POST."""        
        response, data = http_request(
            self.server_config['balance_host'],
            "GET",
            self.server_config["payout_url"] % balance_auth_token,
            use_https=False
        )
        
        if self.is_auth_token_rejected(response):
            data = _("Auth token rejected by server.")
        elif not data:
            data = STR_CONNECTION_ERROR
        else:
            data = _("Withdraw OK")
        wx.CallAfter(self.on_balance_received, data)

    def request_payout_deepbit(self, balance_auth_token):
        """Request payout from deepbit's server via HTTP POST."""
        post_params = dict(id=1,
                           method="request_payout")
        response, data = http_request(
             self.server_config['balance_host'],
             "POST",
             self.server_config['balance_url'] % balance_auth_token,
             json.dumps(post_params),
             {"Content-type": "application/json; charset=utf-8",
              "User-Agent": USER_AGENT}
        )
        if self.is_auth_token_rejected(response):
            data = _("Auth token rejected by server.")
        elif not data:
            data = STR_CONNECTION_ERROR
        else:
            data = _("Withdraw OK")
        wx.CallAfter(self.on_balance_received, data)

    def request_payout_bitpenny(self, withdraw):
        """Request our balance from BitPenny via HTTP POST.

        If withdraw is True, also request a withdrawal.
        """
        post_params = dict(a=self.txt_username.GetValue(), w=int(withdraw))
        response, data = http_request(
             self.server_config['balance_host'],
             "POST",
             self.server_config['balance_url'],
             urllib.urlencode(post_params),
             {"Content-type": "application/x-www-form-urlencoded"}
        )
        if self.is_auth_token_rejected(response):
            data = _("Auth token rejected by server.")
        elif not data:
            data = STR_CONNECTION_ERROR
        elif withdraw:
            data = _("Withdraw OK")
        wx.CallAfter(self.on_balance_received, data)

    def request_balance_eligius(self):
        """Request our balance from Eligius
        """
        response, data = http_request(
             self.server_config['balance_host'],
             "POST",
             self.server_config['balance_url'] % (self.txt_username.GetValue(),),
        )
        if not data:
            data = STR_CONNECTION_ERROR
        try:
            data = json.loads(data)
            data = data['expected'] / 1e8
        except BaseException as e:
            data = str(e)
        wx.CallAfter(self.on_balance_received, data)

    def on_balance_received(self, balance):
        """Set the balance in the GUI."""
        try:
            amt = float(balance)
        except ValueError: # Response was some kind of error
            self.balance_amt.SetLabel(balance)
        else:
            if amt > 0.1:
                self.withdraw.Enable()
            amt_str = format_balance(amt)
            self.balance_amt.SetLabel(amt_str)
        self.Layout()

    # End server specific HTTP code
    ###############################

    def set_name(self, name):
        """Set the label on this miner's tab to name."""
        self.name = name
        if self.summary_name:
            self.summary_name.SetLabel(self.name)
        self.update_tab_name()

    def update_tab_name(self):
        """Update the tab name to reflect modified status."""
        name = self.name
        if self.is_modified:
            name += "*"
        page = self.parent.GetPageIndex(self)
        if page != -1:
            self.parent.SetPageText(page, name)

    def check_if_modified(self, event):
        """Update the title of the tab to have an asterisk if we are modified."""
        self.update_tab_name()
        event.Skip()

    def on_saved(self):
        """Update our last data after a save."""
        self.last_data = self.get_data()
        self.update_tab_name()

    def layout_init(self):
        """Create the sizers for this frame and set up the external text.

        Return the lowest row that is available.
        """
        self.frame_sizer = wx.BoxSizer(wx.VERTICAL)
        self.frame_sizer.Add((20, 10), 0, wx.EXPAND, 0) # Controls top window size
        self.inner_sizer = wx.GridBagSizer(10, 5) # Controls inner window height, width
        self.button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        row = 0
        # if self.is_external_miner:
            # self.inner_sizer.Add(self.external_lbl, (row, 0), flag=LBL_STYLE)
            # self.inner_sizer.Add(self.txt_external, (row, 1), span=(1, 3), flag=wx.EXPAND)
            # row += 1
        return row

    def layout_server_and_website(self, row):
        """Lay out the server and website widgets in the specified row."""
        self.inner_sizer.Add(self.server_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.server, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.website_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.website, (row, 3), flag=wx.ALIGN_CENTER_VERTICAL)
        
    def layout_minertype(self, row):
        """Display which miner is being used"""
        if self.external_path == "CGMINER":
            self.inner_sizer.Add(self.minercgminer_lbl, (row, 0), flag=LBL_STYLE)
        elif self.external_path == "REAPER":
            self.inner_sizer.Add(self.minerreaper_lbl, (row, 0), flag=LBL_STYLE)
        elif self.external_path == "CUDAMINER":
            self.inner_sizer.Add(self.minercudaminer_lbl, (row, 0), flag=LBL_STYLE)
        else:
            self.inner_sizer.Add(self.proxy_lbl, (row, 0), flag=LBL_STYLE)

    def layout_host_and_port(self, row):
        """Lay out the host and port widgets in the specified row."""
        self.inner_sizer.Add(self.host_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_host, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.port_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_port, (row, 3), flag=wx.ALIGN_CENTER_VERTICAL)

    def layout_user_and_pass(self, row):
        """
        Lay out the user and pass widgets in the specified row.
        Also used to help out users with stratum proxy.
        """
        if (self.external_path == "PROXY"):
            self.inner_sizer.Add(self.stratuminfo0_lbl, (row, 0), flag=wx.EXPAND)
        else:
            self.inner_sizer.Add(self.user_lbl, (row, 0), flag=LBL_STYLE)
        if  (self.external_path == "PROXY"):
            self.inner_sizer.Add(self.stratuminfo1_lbl, (row, 1), flag=wx.EXPAND)
        else:
            self.inner_sizer.Add(self.txt_username, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.pass_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_pass, (row, 3), flag=wx.EXPAND)    
    
    def layout_thrcon_worksize(self, row):
        """
        Like it sounds, thread concurrency and worksize boxes.
        """
        self.inner_sizer.Add(self.thrcon_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_thrcon, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.worksize_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_worksize, (row, 3), flag=wx.EXPAND)

    def layout_vectors_intensity(self, row):
        """
        Like it sounds, vector and intensity boxes.
        """
        self.inner_sizer.Add(self.vectors_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_vectors, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.intensity_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_intensity, (row, 3), flag=wx.EXPAND)
        
    def layout_gputhreads_gpusettings(self, row):
        """
        Like it sounds, no. gpu threads and gpu defaults
        """
        self.inner_sizer.Add(self.gputhreads_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_gputhreads, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.gpusettings_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.gpusettings, (row, 3), flag=wx.EXPAND)
        
    def layout_stratum(self, row):
        """
        Like it sounds, stratum boxes.
        """
        self.inner_sizer.Add(self.stratum_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_stratum, (row, 1), flag=wx.EXPAND)
    
    def layout_device_and_flags(self, row):
        """Lay out the device and flags widgets in the specified row.

        Hide the device dropdown if RPCMiner is present since it doesn't use it.
        """
        device_visible = self.is_device_visible
        self.set_widgets_visible([self.device_lbl, self.device_listbox], device_visible)
        if device_visible:
            self.inner_sizer.Add(self.device_lbl, (row, 0), flag=LBL_STYLE)
            self.inner_sizer.Add(self.device_listbox, (row, 1), flag=wx.EXPAND)
        col = 2 * (device_visible)
        self.inner_sizer.Add(self.flags_lbl, (row, col), flag=LBL_STYLE)
        span = (1, 1) if device_visible else (1, 4)
        self.inner_sizer.Add(self.txt_flags, (row, col + 1), span=span, flag=wx.EXPAND)
        
    def layout_affinity(self, row):
        """Lay out the affinity checkboxes in the specified row."""
        self.inner_sizer.Add(self.affinity_lbl, (row, 0))

        affinity_sizer = wx.BoxSizer(wx.HORIZONTAL)
        # for chk in self.affinity_chks:
            # affinity_sizer.Add(chk)
        # self.inner_sizer.Add(affinity_sizer, (row, 1))

    def layout_balance(self, row):
        """Lay out the balance widgets in the specified row."""
        self.inner_sizer.Add(self.balance_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.balance_amt, (row, 1))

    def layout_finish(self):
        """Lay out the buttons and fit the sizer to the window."""
        self.frame_sizer.Add(self.inner_sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.frame_sizer.Add(self.button_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL)
        self.inner_sizer.AddGrowableCol(1)
        self.inner_sizer.AddGrowableCol(3)
        for btn in [self.start, self.balance_refresh, self.withdraw]:
            self.button_sizer.Add(btn, 0, BTN_STYLE, 5)

        # self.set_widgets_visible([self.external_lbl, self.txt_external],
        #                         self.is_external_miner)
        self.SetSizerAndFit(self.frame_sizer)

    def layout_default(self):
        """Lay out a default miner with no custom changes."""
        
        self.user_lbl.SetLabel(STR_USERNAME)
        self.set_widgets_visible(self.hidden_widgets, False)
        self.set_widgets_visible([self.balance_lbl,
                                  self.balance_amt,
                                  self.balance_refresh,
                                  self.withdraw,
                                  self.server,
                                  self.website,
                                  self.server_lbl,
                                  self.website_lbl,
                                  self.interactive_lbl,
                                  self.txt_interactive,
                                  self.texcache_lbl,
                                  self.txt_texcache,
                                  self.singlemem_lbl,
                                  self.txt_singlemem,
                                  self.warpflags_lbl,
                                  self.txt_warpflags,
                                  self.minercudaminer_lbl], False)
        row = self.layout_init()
        # self.layout_server_and_website(row=row)
        
        customs = ["other", "solo"]
        is_custom = self.server.GetStringSelection().lower() in customs
        if is_custom:
            # self.layout_host_and_port(row=row + 1)
            pass
        # Nope - TT
        #else:
        #    self.set_widgets_visible([self.host_lbl, self.txt_host,
        #                              self.port_lbl, self.txt_port], False)
        
        self.set_widgets_visible([self.affinity_lbl], False)

        self.layout_minertype(row=row)
        self.layout_host_and_port(row=row + 1)
        self.layout_user_and_pass(row=row + 2 + int(is_custom))
        self.layout_device_and_flags(row=row + 3 + int(is_custom))
        self.layout_thrcon_worksize(row=row + 4 + int(is_custom))
        self.layout_vectors_intensity(row=row + 5 + int(is_custom))
        self.layout_gputhreads_gpusettings(row=row + 6 + int(is_custom))
        self.layout_stratum(row=row + 7 + int(is_custom))
        # self.layout_affinity(row=row + 7 + int(is_custom))
        
        if self.external_path == "CGMINER":
            self.set_widgets_visible([self.minerreaper_lbl, self.proxy_lbl], False)
        elif self.external_path == "REAPER":
            self.set_widgets_visible([self.minercgminer_lbl, self.proxy_lbl, self.stratum_lbl, self.txt_stratum, self.flags_lbl, self.txt_flags], False)
        else:
            self.set_widgets_visible([self.minercgminer_lbl, self.minerreaper_lbl], False)
            
        if self.external_path == "PROXY":
            self.set_widgets_visible([self.user_lbl, self.pass_lbl, self.device_lbl, self.thrcon_lbl, self.worksize_lbl, self.vectors_lbl, self.intensity_lbl, self.gputhreads_lbl,  self.gpusettings_lbl,  self.stratum_lbl], False)
            self.set_widgets_visible([self.txt_username, self.txt_pass, self.device_listbox, self.txt_thrcon, self.txt_worksize, self.txt_vectors, self.txt_intensity, self.txt_gputhreads, self.gpusettings, self.txt_stratum], False) 
        else:
            self.set_widgets_visible([self.stratuminfo0_lbl, self.stratuminfo1_lbl], False)
       
        self.layout_finish()

    def layout_interactive_texcache(self, row):
        """
        Interactive and use texture cache for cudaminer
        """
        self.inner_sizer.Add(self.interactive_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_interactive, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.texcache_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_texcache, (row, 3), flag=wx.EXPAND)
        
    def layout_singlemem_warpflags(self, row):
        """
        Warp configuration string and use single memory block for cudaminer
        """
        self.inner_sizer.Add(self.singlemem_lbl, (row, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_singlemem, (row, 1), flag=wx.EXPAND)
        self.inner_sizer.Add(self.warpflags_lbl, (row, 2), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_warpflags, (row, 3), flag=wx.EXPAND)
        
    def layout_cudaminer(self):
        """Lay out a default miner with no custom changes."""
        
        self.user_lbl.SetLabel(STR_USERNAME)
        self.set_widgets_visible(self.hidden_widgets, False)
        self.set_widgets_visible([self.balance_lbl,
                                  self.balance_amt,
                                  self.balance_refresh,
                                  self.withdraw,
                                  self.server,
                                  self.website,
                                  self.server_lbl,
                                  self.website_lbl,
                                  self.txt_thrcon,
                                  self.thrcon_lbl,
                                  self.txt_worksize,
                                  self.worksize_lbl,
                                  self.txt_vectors,
                                  self.vectors_lbl,
                                  self.txt_intensity,
                                  self.intensity_lbl,
                                  self.txt_gputhreads,
                                  self.gputhreads_lbl,
                                  self.txt_stratum,
                                  self.stratum_lbl,
                                  self.gpusettings,
                                  self.gpusettings_lbl,
                                  self.stratuminfo0_lbl,
                                  self.stratuminfo1_lbl,
                                  self.proxy_lbl,
                                  self.minercgminer_lbl,
                                  self.minerreaper_lbl], False)
                                  
        row = self.layout_init()
        # self.layout_server_and_website(row=row)
        
        customs = ["other", "solo"]
        is_custom = self.server.GetStringSelection().lower() in customs
        if is_custom:
            # self.layout_host_and_port(row=row + 1)
            pass
        # Nope - TT
        #else:
        #    self.set_widgets_visible([self.host_lbl, self.txt_host,
        #                              self.port_lbl, self.txt_port], False)
        
        self.set_widgets_visible([self.affinity_lbl], False)

        self.layout_minertype(row=row)
        self.layout_host_and_port(row=row + 1)
        self.layout_user_and_pass(row=row + 2 + int(is_custom))
        self.layout_device_and_flags(row=row + 3 + int(is_custom))
        self.layout_interactive_texcache(row=row + 4 + int(is_custom))
        self.layout_singlemem_warpflags(row=row + 5 + int(is_custom))

        self.layout_finish()

    ############################
    # Begin server specific code
    def layout_bitpenny(self):
        """BitPenny doesn't require registration or a password.

        The username is just their receiving address.
        """
        invisible = [self.txt_pass, self.txt_host, self.txt_port,
                     self.pass_lbl, self.host_lbl, self.port_lbl]
        self.set_widgets_visible(invisible, False)
        self.set_widgets_visible([self.extra_info], True)

        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.inner_sizer.Add(self.user_lbl, (row + 1, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_username, (row + 1, 1), span=(1, 3), flag=wx.EXPAND)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.inner_sizer.Add(self.extra_info, (row + 5, 0), span=(1, 4), flag=wx.ALIGN_CENTER_HORIZONTAL)
        self.layout_finish()

        self.extra_info.SetLabel(_("No registration is required - just enter an address and press Start."))
        self.txt_pass.SetValue('poclbm-gui')
        self.user_lbl.SetLabel(_("Address:"))
        add_tooltip(self.txt_username,
            _("Your receiving address for Bitcoins.\nE.g.: 1A94cjRpaPBMV9ZNWFihB5rTFEeihBALgc"))

    def layout_slush(self):
        """Slush's pool uses a separate username for each miner."""
        self.set_widgets_visible([self.host_lbl, self.txt_host,
                                  self.port_lbl, self.txt_port,
                                  self.withdraw, self.extra_info], False)
        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.layout_user_and_pass(row=row + 1)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.layout_finish()

        add_tooltip(self.txt_username,
            _("Your miner username (not your account username).\nExample: Kiv.GPU"))
        add_tooltip(self.txt_pass,
            _("Your miner password (not your account password)."))

    def layout_eligius(self):
        """Eligius doesn't require registration or a password.

        The username is just their receiving address.
        """
        invisible = [self.txt_pass, self.txt_host, self.txt_port,
                     self.withdraw,
                     self.pass_lbl, self.host_lbl, self.port_lbl]
        self.set_widgets_visible(invisible, False)
        self.set_widgets_visible([self.extra_info], True)

        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.inner_sizer.Add(self.user_lbl, (row + 1, 0), flag=LBL_STYLE)
        self.inner_sizer.Add(self.txt_username, (row + 1, 1), span=(1, 3), flag=wx.EXPAND)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.inner_sizer.Add(self.extra_info, (row + 5, 0), span=(1, 4), flag=wx.ALIGN_CENTER_HORIZONTAL)
        self.layout_finish()

        self.extra_info.SetLabel(_("No registration is required - just enter an address and press Start."))
        self.txt_pass.SetValue('x')
        self.user_lbl.SetLabel(_("Address:"))
        add_tooltip(self.txt_username,
            _("Your receiving address for Bitcoins.\nE.g.: 1JMfKKJqtkDPbRRsFSLjX1Cs2dqmjKiwj8"))

    def layout_btcguild(self):
        """BTC Guild has the same layout as slush for now."""
        self.layout_slush()

    def layout_bitcoinserver(self):
        """Bitcoin-Server.de has the same layout as slush for now."""
        self.layout_slush()

    def layout_btcmine(self):
        self.set_widgets_visible([self.host_lbl, self.txt_host,
                                  self.port_lbl, self.txt_port,
                                  self.withdraw, self.extra_info], False)
        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.layout_user_and_pass(row=row + 1)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.layout_finish()

        add_tooltip(self.txt_username,
            _("Your miner username. \nExample: kiv123@kiv123"))
        add_tooltip(self.txt_pass,
            _("Your miner password (not your account password)."))

    def layout_deepbit(self):
        """Deepbit uses an email address for a username."""
        self.set_widgets_visible([self.host_lbl, self.txt_host,
                                  self.port_lbl, self.txt_port,
                                  self.extra_info], False)
        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.layout_user_and_pass(row=row + 1)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.layout_finish()
        add_tooltip(self.txt_username,
            _("The e-mail address you registered with."))
        self.user_lbl.SetLabel(_("Email:"))

    def layout_btcmp(self):
        """Deepbit uses an email address for a username."""
        self.set_widgets_visible([self.host_lbl, self.txt_host,
                                  self.port_lbl, self.txt_port,
                                  self.extra_info], False)
        row = self.layout_init()
        self.layout_server_and_website(row=row)
        self.layout_user_and_pass(row=row + 1)
        self.layout_device_and_flags(row=row + 2)
        self.layout_affinity(row=row + 3)
        self.layout_balance(row=row + 4)
        self.layout_finish()
        add_tooltip(self.txt_username,
            _("Your worker name. Is something in the form of username.workername"))
        self.user_lbl.SetLabel(_("Workername:"))

    def layout_x8s(self):
        """x8s has the same layout as slush for now."""
        self.layout_slush()
    # End server specific code
    ##########################


class GUIMiner(wx.Frame):
    def __init__(self, *args, **kwds):
        wx.Frame.__init__(self, *args, **kwds)
        style = fnb.FNB_X_ON_TAB | fnb.FNB_FF2 | fnb.FNB_HIDE_ON_SINGLE_TAB
        self.nb = fnb.FlatNotebook(self, -1, style=style)

        # Set up notebook context menu
        notebook_menu = wx.Menu()
        ID_RENAME, ID_DUPLICATE = wx.NewId(), wx.NewId()
        notebook_menu.Append(ID_RENAME, _("&Rename..."), _("Rename this miner"))
        notebook_menu.Append(ID_DUPLICATE, _("&Duplicate...", _("Duplicate this miner")))
        self.nb.SetRightClickMenu(notebook_menu)
        self.Bind(wx.EVT_MENU, self.rename_miner, id=ID_RENAME)
        self.Bind(wx.EVT_MENU, self.duplicate_miner, id=ID_DUPLICATE)

        self.console_panel = None
        self.summary_panel = None

        # Servers and defaults are required, it's a fatal error not to have
        # them.
        server_config_path = os.path.join(get_module_path(), 'servers.ini')
        with open(server_config_path) as f:
            data = json.load(f)
            self.servers = data.get('servers')

        defaults_config_path = os.path.join(get_module_path(), 'defaults.ini')
        with open(defaults_config_path) as f:
            self.defaults = json.load(f)
            
        gpusettings_config_path = os.path.join(get_module_path(), 'gpusettings.ini')
        with open(gpusettings_config_path) as f:
            data = json.load(f)
            self.gpusettings_data = data.get('gpusettings')

        self.parse_config()
        self.do_show_opencl_warning = self.config_data.get('show_opencl_warning', True)
        self.console_max_lines = self.config_data.get('console_max_lines', 5000)

        ID_NEW_EXTERNAL, ID_NEW_PHOENIX, ID_NEW_CGMINER, ID_NEW_REAPER, ID_NEW_CUDAMINER, ID_NEW_PROXY, ID_NEW_UFASOFT = wx.NewId(), wx.NewId(), wx.NewId(), wx.NewId(), wx.NewId(), wx.NewId(), wx.NewId()
        self.menubar = wx.MenuBar()
        file_menu = wx.Menu()
        new_menu = wx.Menu()
        #new_menu.Append(wx.ID_NEW, _("&New OpenCL miner..."), _("Create a new OpenCL miner (default for ATI cards)"), wx.ITEM_NORMAL)
        #new_menu.Append(ID_NEW_PHOENIX, _("New Phoenix miner..."), _("Create a new Phoenix miner (for some ATI cards)"), wx.ITEM_NORMAL)
        new_menu.Append(ID_NEW_CGMINER, _("New CG miner..."), _("Create a new CGMiner"), wx.ITEM_NORMAL)
        new_menu.Append(ID_NEW_REAPER, _("New reaper miner..."), _("Create a new reaper miner"), wx.ITEM_NORMAL)
        new_menu.Append(ID_NEW_CUDAMINER, _("New CUDA miner..."), _("Create a new CUDA miner"), wx.ITEM_NORMAL)
        new_menu.Append(ID_NEW_PROXY, _("New stratum proxy..."), _("Create a new stratum proxy"), wx.ITEM_NORMAL)
        #new_menu.Append(ID_NEW_UFASOFT, _("New Ufasoft CPU miner..."), _("Create a new Ufasoft miner (for CPUs)"), wx.ITEM_NORMAL)
        #new_menu.Append(ID_NEW_EXTERNAL, _("New &other miner..."), _("Create a new custom miner (requires external program)"), wx.ITEM_NORMAL)
        file_menu.AppendMenu(wx.NewId(), _('&New miner'), new_menu)
        file_menu.Append(wx.ID_SAVE, _("&Save settings"), _("Save your settings"), wx.ITEM_NORMAL)
        file_menu.Append(wx.ID_OPEN, _("&Load settings"), _("Load stored settings"), wx.ITEM_NORMAL)
        file_menu.Append(wx.ID_EXIT, _("Quit"), STR_QUIT, wx.ITEM_NORMAL)
        self.menubar.Append(file_menu, _("&File"))

        ID_SUMMARY, ID_CONSOLE = wx.NewId(), wx.NewId()
        view_menu = wx.Menu()
        view_menu.Append(ID_SUMMARY, _("Show summary"), _("Show summary of all miners"), wx.ITEM_NORMAL)
        view_menu.Append(ID_CONSOLE, _("Show console"), _("Show console logs"), wx.ITEM_NORMAL)
        self.menubar.Append(view_menu, _("&View"))

        ID_SOLO, ID_PATHS, ID_BLOCKCHAIN_PATH, ID_LAUNCH = wx.NewId(), wx.NewId(), wx.NewId(), wx.NewId()
        solo_menu = wx.Menu()
        solo_menu.Append(ID_SOLO, _("&Create solo password..."), _("Configure a user/pass for solo mining"), wx.ITEM_NORMAL)
        solo_menu.Append(ID_PATHS, _("&Set Bitcoin client path..."), _("Set the location of the official Bitcoin client"), wx.ITEM_NORMAL)
        solo_menu.Append(ID_BLOCKCHAIN_PATH, _("&Set Bitcoin data directory..."), _("Set the location of the bitcoin data directory containing the blockchain and wallet"), wx.ITEM_NORMAL)
        solo_menu.Append(ID_LAUNCH, _("&Launch Bitcoin client as server"), _("Launch the official Bitcoin client as a server for solo mining"), wx.ITEM_NORMAL)
        self.menubar.Append(solo_menu, _("&Solo utilities"))

        ID_START_MINIMIZED = wx.NewId()
        self.options_menu = wx.Menu()
        self.start_minimized_chk = self.options_menu.Append(ID_START_MINIMIZED, _("Start &minimized"), _("Start the GUI minimized to the tray."), wx.ITEM_CHECK)
        self.options_menu.Check(ID_START_MINIMIZED, self.config_data.get('start_minimized', False))
        self.menubar.Append(self.options_menu, _("&Options"))

        ID_CHANGE_LANGUAGE = wx.NewId()
        lang_menu = wx.Menu()
        lang_menu.Append(ID_CHANGE_LANGUAGE, _("&Change language..."), "", wx.ITEM_NORMAL)
        self.menubar.Append(lang_menu, _("Language"))

        ID_DONATE_SMALL = wx.NewId()
        donate_menu = wx.Menu()
        donate_menu.Append(ID_DONATE_SMALL, _("&Donate..."), _("Donate Litecoins to support GUIMiner development"))
        self.menubar.Append(donate_menu, _("&Donate"))

        help_menu = wx.Menu()
        help_menu.Append(wx.ID_ABOUT, _("&About..."), STR_ABOUT, wx.ITEM_NORMAL)

        self.menubar.Append(help_menu, _("&Help"))
        self.SetMenuBar(self.menubar)
        self.statusbar = self.CreateStatusBar(2, 0)

        try:
            self.bitcoin_executable = os.path.join(os.getenv("PROGRAMFILES"), "Bitcoin", "bitcoin-qt.exe")
        except:
            self.bitcoin_executable = "" # TODO: where would Bitcoin probably be on Linux/Mac?

        try:
            self.blockchain_directory = os.path.join(os.getenv("APPDATA"), "Bitcoin")
        except:
            self.blockchain_directory = ""
          
        
        try:
            self.tbicon = GUIMinerTaskBarIcon(self)
        except:
            logging.error(_("Failed to load taskbar icon; continuing."))

        self.set_properties()

        try:
            self.devices = get_opencl_devices()
        except:
            self.devices = []
            file_menu.Enable(wx.ID_NEW, False)
            file_menu.SetHelpString(wx.ID_NEW, _("OpenCL not found - can't add a OpenCL miner"))

            if self.do_show_opencl_warning:
                dialog = OpenCLWarningDialog(self)
                dialog.ShowModal()
                self.do_show_opencl_warning = not dialog.is_box_checked()

        self.Bind(wx.EVT_MENU, self.name_new_profile, id=wx.ID_NEW)
        #self.Bind(wx.EVT_MENU, self.new_phoenix_profile, id=ID_NEW_PHOENIX)
        self.Bind(wx.EVT_MENU, self.new_cgminer_profile, id=ID_NEW_CGMINER)
        self.Bind(wx.EVT_MENU, self.new_ufasoft_profile, id=ID_NEW_UFASOFT)
        self.Bind(wx.EVT_MENU, self.new_reaper_profile, id=ID_NEW_REAPER)
        self.Bind(wx.EVT_MENU, self.new_cudaminer_profile, id=ID_NEW_CUDAMINER)
        self.Bind(wx.EVT_MENU, self.new_proxy_profile, id=ID_NEW_PROXY)
        self.Bind(wx.EVT_MENU, self.new_external_profile, id=ID_NEW_EXTERNAL)
        self.Bind(wx.EVT_MENU, self.save_config, id=wx.ID_SAVE)
        self.Bind(wx.EVT_MENU, self.load_config, id=wx.ID_OPEN)
        self.Bind(wx.EVT_MENU, self.on_menu_exit, id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, self.set_official_client_path, id=ID_PATHS)
        self.Bind(wx.EVT_MENU, self.set_blockchain_directory, id=ID_BLOCKCHAIN_PATH)
        self.Bind(wx.EVT_MENU, self.show_console, id=ID_CONSOLE)
        self.Bind(wx.EVT_MENU, self.show_summary, id=ID_SUMMARY)
        self.Bind(wx.EVT_MENU, self.show_about_dialog, id=wx.ID_ABOUT)
        self.Bind(wx.EVT_MENU, self.create_solo_password, id=ID_SOLO)
        self.Bind(wx.EVT_MENU, self.launch_solo_server, id=ID_LAUNCH)
        self.Bind(wx.EVT_MENU, self.on_change_language, id=ID_CHANGE_LANGUAGE)
        self.Bind(wx.EVT_MENU, self.on_donate, id=ID_DONATE_SMALL)
        self.Bind(wx.EVT_CLOSE, self.on_close)        
        self.Bind(wx.EVT_ICONIZE, self.on_iconize)
        self.Bind(fnb.EVT_FLATNOTEBOOK_PAGE_CLOSING, self.on_page_closing)
        self.Bind(fnb.EVT_FLATNOTEBOOK_PAGE_CLOSED, self.on_page_closed)
        self.Bind(fnb.EVT_FLATNOTEBOOK_PAGE_CHANGED, self.on_page_changed)

        self.load_config()
        self.do_layout()

        if not self.start_minimized_chk.IsChecked():
            self.Show()
            
    def on_iconize(self, event):
        if event.Iconized() and sys.platform == 'win32':
            self.Hide() # On minimize, hide from taskbar.
        else:
            self.Show()

    def set_properties(self):
        self.SetIcons(get_icon_bundle())
        self.SetTitle(_("GUIMiner-scrypt alpha"))
        self.statusbar.SetStatusWidths([-1, 125])
        statusbar_fields = ["", STR_NOT_STARTED]
        for i in range(len(statusbar_fields)):
            self.statusbar.SetStatusText(statusbar_fields[i], i)

    def do_layout(self):
        self.vertical_sizer = wx.BoxSizer(wx.VERTICAL)
        self.vertical_sizer.Add(self.nb, 1, wx.EXPAND, 20)
        self.SetSizer(self.vertical_sizer)
        self.vertical_sizer.SetSizeHints(self)
        self.SetSizerAndFit(self.vertical_sizer)
        self.Layout()

    @property
    def profile_panels(self):
        """Return a list of currently available MinerTab."""
        pages = [self.nb.GetPage(i) for i in range(self.nb.GetPageCount())]
        return [p for p in pages if
                p != self.console_panel and p != self.summary_panel]

    def add_profile(self, data={}):
        """Add a new MinerTab to the list of tabs."""
        panel = MinerTab(self.nb, -1, self.devices, self.servers,
                             self.defaults, self.gpusettings_data, self.statusbar, data)
        self.nb.AddPage(panel, panel.name)
        # The newly created profile should have focus.
        self.nb.EnsureVisible(self.nb.GetPageCount() - 1)

        if self.summary_panel is not None:
            self.summary_panel.add_miners_to_grid() # Show new entry on summary
        return panel

    def message(self, *args, **kwargs):
        """Utility method to show a message dialog and return their choice."""
        dialog = wx.MessageDialog(self, *args, **kwargs)
        retval = dialog.ShowModal()
        dialog.Destroy()
        return retval

    def name_new_profile(self, event=None, extra_profile_data={}):
        """Prompt for the new miner's name."""
        dialog = wx.TextEntryDialog(self, _("Name this miner:"), _("New miner"))
        if dialog.ShowModal() == wx.ID_OK:
            name = dialog.GetValue().strip()
            if not name: name = _("Untitled")
            data = extra_profile_data.copy()
            data['name'] = name
            self.add_profile(data)

    def new_external_profile(self, event):
        """Prompt for an external miner path, then create a miner.

        On Windows we validate against legal miners; on Linux they can pick
        whatever they want.
        """
        wildcard = _('External miner (*.exe)|*.exe|(*.py)|*.py') if sys.platform == 'win32' else '*.*'
        dialog = wx.FileDialog(self,
                               _("Select external miner:"),
                               defaultDir=os.path.join(get_module_path(), 'miners'),
                               defaultFile="",
                               wildcard=wildcard,
                               style=wx.OPEN)
        if dialog.ShowModal() != wx.ID_OK:
            return

        if sys.platform == 'win32' and dialog.GetFilename() not in SUPPORTED_BACKENDS:
            self.message(
                _("Unsupported external miner %(filename)s. Supported are: %(supported)s") % \
                  dict(filename=dialog.GetFilename(), supported='\n'.join(SUPPORTED_BACKENDS)),
                _("Miner not supported"), wx.OK | wx.ICON_ERROR)
            return
        path = os.path.join(dialog.GetDirectory(), dialog.GetFilename())
        dialog.Destroy()
        self.name_new_profile(extra_profile_data=dict(external_path="CGMINER"))

    def new_phoenix_profile(self, event):
        """Create a new miner using the Phoenix OpenCL miner backend."""
        path = os.path.join(get_module_path(), 'phoenix.exe')
        self.name_new_profile(extra_profile_data=dict(external_path="CGMINER"))

    def new_cgminer_profile(self, event):
        """Create a new miner using the Cgminer OpenCL miner backend."""
        path = os.path.join(get_module_path(), 'cgminer.exe')
        self.name_new_profile(extra_profile_data=dict(external_path="CGMINER"))

    def new_ufasoft_profile(self, event):
        """Create a new miner using the Ufasoft CPU miner backend."""
        # path = os.path.join(get_module_path(), 'miners', 'ufasoft', 'bitcoin-miner.exe')
        self.name_new_profile(extra_profile_data=dict(external_path="CGMINER"))

    def new_reaper_profile(self, event):
        """Create a new miner using the REAPER GPU miner backend."""
        # path = os.path.join(get_module_path(), 'miners', 'puddinpop', 'rpcminer-cuda.exe')
        self.name_new_profile(extra_profile_data=dict(external_path="REAPER"))
        
    def new_cudaminer_profile(self, event):
        """Create a new miner using the cudaminer GPU miner backend."""
        self.name_new_profile(extra_profile_data=dict(external_path="CUDAMINER"))
        
    def new_proxy_profile(self, event):
        """Create a stratum proxy backend."""
        # path = os.path.join(get_module_path(), 'miners', 'puddinpop', 'rpcminer-cuda.exe')
        self.name_new_profile(extra_profile_data=dict(external_path="PROXY"))

    def get_storage_location(self):
        """Get the folder and filename to store our JSON config."""
        if sys.platform == 'win32':
            folder = os.path.join(os.environ['AppData'], 'poclbm')
            config_filename = os.path.join(folder, 'poclbm_scrypt.ini')
        else: # Assume linux? TODO test
            folder = os.environ['HOME']
            config_filename = os.path.join(folder, '.poclbm')
        return folder, config_filename

    def on_close(self, event):
        """Minimize to tray if they click "close" but exit otherwise.

        On closing, stop any miners that are currently working.
        """
        if event.CanVeto():
            self.Hide()
            event.Veto()
        else:
            if any(p.is_modified for p in self.profile_panels):
                dialog = wx.MessageDialog(self, _('Do you want to save changes?'), _('Save'),
                    wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION)
                retval = dialog.ShowModal()
                dialog.Destroy()
                if retval == wx.ID_YES:
                    self.save_config()

            if self.console_panel is not None:
                self.console_panel.on_close()
            if self.summary_panel is not None:
                self.summary_panel.on_close()
            for p in self.profile_panels:
                p.on_close()
            if self.tbicon is not None:
                self.tbicon.RemoveIcon()
                self.tbicon.timer.Stop()
                self.tbicon.Destroy()
            event.Skip()

    def save_config(self, event=None):
        """Save the current miner profiles to our config file in JSON format."""
        folder, config_filename = self.get_storage_location()
        mkdir_p(folder)
        profile_data = [p.get_data() for p in self.profile_panels]
        config_data = dict(show_console=self.is_console_visible(),
                           show_summary=self.is_summary_visible(),
                           profiles=profile_data,
                           bitcoin_executable=self.bitcoin_executable,
                           blockchain_directory=self.blockchain_directory,
                           show_opencl_warning=self.do_show_opencl_warning,
                           start_minimized=self.start_minimized_chk.IsChecked(),
                           console_max_lines=self.console_max_lines,
                           window_position=list(self.GetRect()))
        logger.debug(_('Saving: ') + json.dumps(config_data))
        try:
            with open(config_filename, 'w') as f:
                json.dump(config_data, f, indent=4)
        except IOError:
            self.message(
                _("Couldn't write save file %s.\nCheck the location is writable.") % config_filename,
                _("Save unsuccessful"), wx.OK | wx.ICON_ERROR)
        else:
            self.message(_("Profiles saved OK to %s.") % config_filename,
                      _("Save successful"), wx.OK | wx.ICON_INFORMATION)
            for p in self.profile_panels:
                p.on_saved()

    def parse_config(self):
        """Set self.config_data to a dictionary of config values."""
        self.config_data = {}

        try:
            config_filename = self.get_storage_location()[1]
            if os.path.exists(config_filename):
                with open(config_filename) as f:
                    self.config_data.update(json.load(f))
                logger.debug(_('Loaded: %s') % json.dumps(self.config_data))
        except ValueError:
            self.message(
                _("Your settings saved at:\n %s\nare corrupt or could not be read.\nDeleting this file or saving over it may solve the problem." % config_filename),
                _("Error"), wx.ICON_ERROR)

    def load_config(self, event=None):
        """Load JSON profile info from the config file."""
        self.parse_config()

        config_data = self.config_data
        executable = config_data.get('bitcoin_executable', None)
        if executable is not None:
            self.bitcoin_executable = executable
            
        blockchain_directory = config_data.get('blockchain_directory', None)
        if blockchain_directory is not None:
            self.blockchain_directory = blockchain_directory

        # Shut down any existing miners before they get clobbered
        if(any(p.is_mining for p in self.profile_panels)):
            result = self.message(
                _("Loading profiles will stop any currently running miners. Continue?"),
                _("Load profile"), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION)
            if result == wx.ID_NO:
                return
        for p in reversed(self.profile_panels):
            p.on_close()
            self.nb.DeletePage(self.nb.GetPageIndex(p))

        # If present, summary should be the leftmost tab on startup.
        if config_data.get('show_summary', False):
            self.show_summary()

        profile_data = config_data.get('profiles', [])
        for d in profile_data:
            self.add_profile(d)

        if not any(profile_data):
            self.add_profile() # Create a default one using defaults.ini

        if config_data.get('show_console', False):
            self.show_console()
            
        window_position = config_data.get('window_position')
        if window_position:
            self.SetRect(window_position)

        for p in self.profile_panels:
            if p.autostart:
                p.start_mining()

    def set_official_client_path(self, event):
        """Set the path to the official Bitcoin client."""
        wildcard = "*.exe" if sys.platform == 'win32' else '*.*'
        dialog = wx.FileDialog(self,
                               _("Select path to Bitcoin.exe"),
                               defaultFile="bitcoin-qt.exe",
                               wildcard=wildcard,
                               style=wx.OPEN)
        if dialog.ShowModal() == wx.ID_OK:
            path = os.path.join(dialog.GetDirectory(), dialog.GetFilename())
            if os.path.exists(path):
                self.bitcoin_executable = path
        dialog.Destroy()
        
    def set_blockchain_directory(self, event):
        """Set the path to the blockchain data directory."""
        defaultPath = os.path.join(os.getenv("APPDATA"), "Bitcoin")
        dialog = wx.DirDialog(self,
                              _("Select path to blockchain"),
                              defaultPath=defaultPath,
                              style=wx.DD_DIR_MUST_EXIST)
        if dialog.ShowModal() == wx.ID_OK:
            path = dialog.GetPath()
            if os.path.exists(path):
                self.blockchain_directory = path
        dialog.Destroy()   

    def show_about_dialog(self, event):
        """Show the 'about' dialog."""
        dialog = AboutGuiminer(self, -1, _('About'))
        dialog.ShowModal()
        dialog.Destroy()

    def on_page_closing(self, event):
        """Handle a tab closing event.

        If they are closing a special panel, we have to shut it down.
        If the tab has a miner running in it, we have to stop the miner
        before letting the tab be removed.
        """
        p = self.nb.GetPage(event.GetSelection())

        if p == self.console_panel:
            self.console_panel.on_close()
            self.console_panel = None
            event.Skip()
            return
        if p == self.summary_panel:
            self.summary_panel.on_close()
            self.summary_panel = None
            event.Skip()
            return

        if p.is_mining:
            result = self.message(
                _("Closing this miner will stop it. Continue?"),
                _("Close miner"),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION)
            if result == wx.ID_NO:
                event.Veto()
                return
        p.on_close()
        event.Skip() # OK to close the tab now

    def on_page_closed(self, event):
        if self.summary_panel is not None:
            self.summary_panel.add_miners_to_grid() # Remove miner summary

    def on_page_changed(self, event):
        """Handle a tab change event.

        Ensures the status bar shows the status of the tab that has focus.
        """
        p = self.nb.GetPage(event.GetSelection())
        p.on_focus()

    def launch_solo_server(self, event):
        """Launch the official bitcoin client in server mode.

        This allows poclbm to connect to it for mining solo.
        """
        if self.blockchain_directory and os.path.exists(self.blockchain_directory):
            datadir = " -datadir=%s" % self.blockchain_directory
        else:
            datadir = ""
        try:
            subprocess.Popen(self.bitcoin_executable + " -server" + datadir)
        except OSError:
            self.message(
                _("Couldn't find Bitcoin at %s. Is your path set correctly?") % self.bitcoin_executable,
                _("Launch failed"), wx.ICON_ERROR | wx.OK)
            return
        self.message(
            _("The Bitcoin client will now launch in server mode.\nOnce it connects to the network and downloads the block chain, you can start a miner in 'solo' mode."),
            _("Launched ok."),
            wx.OK)

    def create_solo_password(self, event):
        """Prompt the user for login credentials to the bitcoin client.

        These are required to connect to the client over JSON-RPC and are
        stored in 'bitcoin.conf'.
        """
        if sys.platform == 'win32':
            filename = os.path.join(os.getenv("APPDATA"), "Bitcoin", "bitcoin.conf")
        else: # Assume Linux for now TODO test
            filename = os.path.join(os.getenv('HOME'), ".bitcoin")
        if os.path.exists(filename):
            result = self.message(
                _("%s already exists. Overwrite?") % filename,
                _("bitcoin.conf already exists."),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION)
            if result == wx.ID_NO:
                return

        dialog = SoloPasswordRequest(self, _('Enter password'))
        result = dialog.ShowModal()
        dialog.Destroy()
        if result == wx.ID_CANCEL:
            return

        with open(filename, "w") as f:
            f.write('\nrpcuser=%s\nrpcpassword=%s\nrpcallowip=*' % dialog.get_value())
            f.close()

        self.message(_("Wrote bitcoin config ok."), _("Success"), wx.OK)

    def is_console_visible(self):
        """Return True if the console is visible."""
        return self.nb.GetPageIndex(self.console_panel) != -1

    def show_console(self, event=None):
        """Show the console log in its own tab."""
        if self.is_console_visible():
            return # Console already shown
        self.console_panel = ConsolePanel(self, self.console_max_lines)
        self.nb.AddPage(self.console_panel, _("Console"))
        self.nb.EnsureVisible(self.nb.GetPageCount() - 1)

    def is_summary_visible(self):
        """Return True if the summary is visible."""
        return self.nb.GetPageIndex(self.summary_panel) != -1

    def show_summary(self, event=None):
        """Show the summary window in its own tab."""
        if self.is_summary_visible():
            return
        self.summary_panel = SummaryPanel(self)
        self.nb.AddPage(self.summary_panel, _("Summary"))
        index = self.nb.GetPageIndex(self.summary_panel)
        self.nb.SetSelection(index)

    def on_menu_exit(self, event):
        self.Close(force=True)

    def rename_miner(self, event):
        """Change the name of a miner as displayed on the tab."""
        p = self.nb.GetPage(self.nb.GetSelection())
        if p not in self.profile_panels:
            return

        dialog = wx.TextEntryDialog(self, _("Rename to:"), _("Rename miner"))
        if dialog.ShowModal() == wx.ID_OK:
            p.set_name(dialog.GetValue().strip())

    def duplicate_miner(self, event):
        """Duplicate the current miner to another miner."""
        p = self.nb.GetPage(self.nb.GetSelection())
        if p not in self.profile_panels:
            return        
        self.name_new_profile(event=None, extra_profile_data=p.get_data())

    def on_change_language(self, event):
        dialog = ChangeLanguageDialog(self, _('Change language'), language)
        result = dialog.ShowModal()
        dialog.Destroy()
        if result == wx.ID_CANCEL:
            return

        language_name = dialog.get_value()
        update_language(LANGUAGES[language_name])
        save_language()

    def on_donate(self, event):
        dialog = DonateDialog(self, -1, _('Donate'))
        dialog.ShowModal()
        dialog.Destroy()

class DonateDialog(wx.Dialog):
    """About dialog for the app with a donation address."""
    DONATE_TEXT = "If this software helped you, please consider contributing to its development." \
                  "\nSend donations to:  %(address)s"
    def __init__(self, parent, id, title):
        wx.Dialog.__init__(self, parent, id, title)
        vbox = wx.BoxSizer(wx.VERTICAL)

        text = DonateDialog.DONATE_TEXT % dict(address=DONATION_ADDRESS)
        self.about_text = wx.StaticText(self, -1, text)
        self.copy_btn = wx.Button(self, -1, _("Copy address to clipboard"))
        vbox.Add(self.about_text, 0, wx.ALL, 10)
        vbox.Add(self.copy_btn, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
        self.SetSizerAndFit(vbox)

        self.copy_btn.Bind(wx.EVT_BUTTON, self.on_copy)

    def on_copy(self, event):
        """Copy the donation address to the clipboard."""
        if wx.TheClipboard.Open():
            data = wx.TextDataObject()
            data.SetText(DONATION_ADDRESS)
            wx.TheClipboard.SetData(data)
        wx.TheClipboard.Close()
        

class ChangeLanguageDialog(wx.Dialog):
    """Dialog prompting the user to change languages."""
    def __init__(self, parent, title, current_language):
        style = wx.DEFAULT_DIALOG_STYLE
        vbox = wx.BoxSizer(wx.VERTICAL)
        wx.Dialog.__init__(self, parent, -1, title, style=style)
        self.lbl = wx.StaticText(self, -1,
            _("Choose language (requires restart to take full effect)"))
        vbox.Add(self.lbl, 0, wx.ALL, 10)
        self.language_choices = wx.ComboBox(self, -1,
                                            choices=sorted(LANGUAGES.keys()),
                                            style=wx.CB_READONLY)

        self.language_choices.SetStringSelection(LANGUAGES_REVERSE[current_language])

        vbox.Add(self.language_choices, 0, wx.ALL, 10)
        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(buttons, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
        self.SetSizerAndFit(vbox)

    def get_value(self):
        return self.language_choices.GetStringSelection()


class SoloPasswordRequest(wx.Dialog):
    """Dialog prompting user for login credentials for solo mining."""
    def __init__(self, parent, title):
        style = wx.DEFAULT_DIALOG_STYLE
        vbox = wx.BoxSizer(wx.VERTICAL)
        wx.Dialog.__init__(self, parent, -1, title, style=style)
        self.user_lbl = wx.StaticText(self, -1, STR_USERNAME)
        self.txt_username = wx.TextCtrl(self, -1, "")
        self.pass_lbl = wx.StaticText(self, -1, STR_PASSWORD)
        self.txt_pass = wx.TextCtrl(self, -1, "", style=wx.TE_PASSWORD)
        grid_sizer_1 = wx.FlexGridSizer(2, 2, 5, 5)
        grid_sizer_1.Add(self.user_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL, 0)
        grid_sizer_1.Add(self.txt_username, 0, wx.EXPAND, 0)
        grid_sizer_1.Add(self.pass_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL, 0)
        grid_sizer_1.Add(self.txt_pass, 0, wx.EXPAND, 0)
        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(grid_sizer_1, wx.EXPAND | wx.ALL, 10)
        vbox.Add(buttons)
        self.SetSizerAndFit(vbox)

    def get_value(self):
        """Return the (username, password) supplied by the user."""
        return self.txt_username.GetValue(), self.txt_pass.GetValue()


class BalanceAuthRequest(wx.Dialog):
    """Dialog prompting user for an auth token to refresh their balance."""
    instructions = \
_("""Click the link below to log in to the pool and get a special token.
This token lets you securely check your balance.
To remember this token for the future, save your miner settings.""")
    def __init__(self, parent, url):
        style = wx.DEFAULT_DIALOG_STYLE
        vbox = wx.BoxSizer(wx.VERTICAL)
        wx.Dialog.__init__(self, parent, -1, STR_REFRESH_BALANCE, style=style)
        self.instructions = wx.StaticText(self, -1, BalanceAuthRequest.instructions)
        self.website = hyperlink.HyperLinkCtrl(self, -1, url)
        self.txt_token = wx.TextCtrl(self, -1, _("(Paste token here)"))
        buttons = self.CreateButtonSizer(wx.OK | wx.CANCEL)

        vbox.AddMany([
            (self.instructions, 0, wx.ALL, 10),
            (self.website, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10),
            (self.txt_token, 0, wx.EXPAND | wx.ALIGN_CENTER_HORIZONTAL, 10),
            (buttons, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
        ])
        self.SetSizerAndFit(vbox)

    def get_value(self):
        """Return the auth token supplied by the user."""
        return self.txt_token.GetValue()


class AboutGuiminer(wx.Dialog):
    """About dialog for the app with a donation address."""
    
    def __init__(self, parent, id, title):
        wx.Dialog.__init__(self, parent, id, title)
        vbox = wx.BoxSizer(wx.VERTICAL)

        text = ABOUT_TEXT % dict(version=__version__,
                                 address=DONATION_ADDRESS)
        self.about_text = wx.StaticText(self, -1, text)
        self.copy_btn = wx.Button(self, -1, _("Copy address to clipboard"))
        vbox.Add(self.about_text)
        vbox.Add(self.copy_btn, 0, wx.ALIGN_BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 0)
        self.SetSizerAndFit(vbox)

        self.copy_btn.Bind(wx.EVT_BUTTON, self.on_copy)

    def on_copy(self, event):
        """Copy the donation address to the clipboard."""
        if wx.TheClipboard.Open():
            data = wx.TextDataObject()
            data.SetText(DONATION_ADDRESS)
            wx.TheClipboard.SetData(data)
        wx.TheClipboard.Close()


class OpenCLWarningDialog(wx.Dialog):
    """Warning dialog when a user does not have OpenCL installed."""
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent, -1, _("No OpenCL devices found."))
        vbox = wx.BoxSizer(wx.VERTICAL)
        self.message = wx.StaticText(self, -1,
 _("""No OpenCL devices were found.
 If you only want to mine using CPU or CUDA, you can ignore this message.
 If you want to mine on ATI graphics cards, you may need to install the ATI Stream
 SDK, or your GPU may not support OpenCL."""))
        vbox.Add(self.message, 0, wx.ALL, 10)

        hbox = wx.BoxSizer(wx.HORIZONTAL)

        self.no_show_chk = wx.CheckBox(self, -1)
        hbox.Add(self.no_show_chk)
        self.no_show_txt = wx.StaticText(self, -1, _("Don't show this message again"))
        hbox.Add((5, 0))
        hbox.Add(self.no_show_txt)
        vbox.Add(hbox, 0, wx.ALL, 10)
        buttons = self.CreateButtonSizer(wx.OK)
        vbox.Add(buttons, 0, wx.ALIGN_BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 0)
        self.SetSizerAndFit(vbox)

    def is_box_checked(self):
        return self.no_show_chk.GetValue()


def run():
    try:
        frame_1 = GUIMiner(None, -1, "")
        app.SetTopWindow(frame_1)
        app.MainLoop()
    except:
        logging.exception("Exception:")
        raise


if __name__ == "__main__":
    run()
