"""MyFlyMouse -> диктовка Claude.

Кнопка микрофона на пульте (HID Consumer 0x00CF) нажимает мышью на значок
микрофона в окне Claude. Пульт не сообщает об отпускании кнопки, поэтому запись
останавливается вторым нажатием на пульт или после паузы в речи.

Значок микрофона скрипт каждый раз находит сам через UI Automation (по названию
кнопки), поэтому положение и размер окна Claude не важны, калибровка не нужна.

  python voice_remote.py
"""
import collections, ctypes, json, os, queue, re, subprocess, sys, threading, time
from ctypes import wintypes

# до любых вызовов user32: все координаты в физических пикселях, как у UI Automation
ctypes.windll.shcore.SetProcessDpiAwareness(2)

import comtypes.client
import hid
import numpy as np
import sounddevice as sd

FROZEN = bool(getattr(sys, "frozen", False))      # собранная программа AirDictate.exe
# рядом лежат настройки и журнал: у .exe это папка с ним, у скрипта папка скрипта
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
# картинки и модели лежат рядом с кодом; у собранной версии это папка _internal (sys._MEIPASS)
ASSETS = os.path.join(getattr(sys, "_MEIPASS", HERE), "assets")
CFG_PATH = os.path.join(HERE, "config.json")
STOP_PATTERN = "остановить диктовку|stop dictation|stop recording"

DEFAULTS = {
    "vid": 0x4842, "pid": 0x0001,
    "audio_device": "USBAudio2.0",   # микрофон пульта, по нему ловим паузу в речи
    "speech_on": 0.004,              # уровень, выше которого считаем, что вы говорите
    "silence_off": 0.0015,           # ниже этого уровня - тишина
    "silence_seconds": 2.2,          # сколько тишины после речи = конец записи
    "no_speech_timeout": 1.5,        # если речи так и не было - отмена записи
    "max_seconds": 120.0,
    "press_enter": True,             # отправить сообщение после диктовки
    "enter_stable_seconds": 0.6,     # Enter, когда текст в поле не менялся столько секунд
    "enter_max_wait": 5.0,           # но не ждём распознавание дольше этого
    "cursor_lock_seconds": 1.5,     # сколько держать курсор запертым над кнопкой (0 - до конца записи)
    # имя кнопки диктовки в интерфейсе Claude (регулярное выражение)
    "mic_button_pattern": "удерживайте для записи|hold to (record|dictate)|push to talk",
    # кнопка записи, которой научил мастер: {"type": "hid", "vid", "pid", "usage_page", "report": [..]}
    # или {"type": "key", "vk": ..}. None = стандартная кнопка микрофона G10S Pro (HID Consumer 0x00CF)
    "trigger": None,
    "setup_done": False,
    "min_hold_seconds": 0.8,         # минимум, сколько держим кнопку мыши на микрофоне (короче Claude считает «тапом»)
    "stale_watch_seconds": 4.0,      # сколько секунд после остановки сторож ищет «застрявшую» диктовку в Claude
}

user32 = ctypes.windll.user32
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
KEYEVENTF_KEYUP, VK_RETURN, VK_MENU, VK_CONTROL = 0x0002, 0x0D, 0x12, 0x11


LOG_PATH = os.path.join(HERE, "voice_remote.log")
HISTORY_PATH = os.path.join(HERE, "sent_history.json")
HISTORY_LIMIT = 300


def norm_text(t):
    """Сравниваем тексты без разницы в пробелах и переводах строк."""
    return " ".join((t or "").split())


def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return [norm_text(x) for x in json.load(f) if norm_text(x)]
    except Exception:
        return []


def save_history(items):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(items[-HISTORY_LIMIT:], f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("не удалось сохранить историю:", repr(e))


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def claude_windows():
    """Все основные окна десктопного Claude (процесс claude.exe, класс Chrome_WidgetWin_1), сверху вниз по Z-порядку.
    Окна сессий могут называться по-разному, поэтому по заголовку не фильтруем, а берём крупные видимые окна;
    спрятанное в трей окно распознаём по заголовку «Claude»."""
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1" or user32.GetWindow(hwnd, 4):   # GW_OWNER: у главных окон владельца нет
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        visible, iconic = bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd))
        big = (r.right - r.left) >= 400 and (r.bottom - r.top) >= 300
        if not ((visible and (big or iconic)) or buf.value == "Claude"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        path = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        if path.value.lower().endswith("claude.exe"):
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def claude_window():
    """Окно Claude, в котором работаем: активное, а если активно другое приложение, то самое верхнее
    (недавно использованное) из видимых окон Claude; потом свёрнутые; в конце спрятанные в трей."""
    wins = claude_windows()
    if not wins:
        return None
    fg = user32.GetForegroundWindow()
    if fg in wins:
        return fg
    for h in wins:
        if user32.IsWindowVisible(h) and not user32.IsIconic(h):
            return h
    for h in wins:
        if user32.IsIconic(h):
            return h
    return wins[0]


def press_keys(*vks):
    """Нажать и отпустить сочетание, например press_keys(VK_CONTROL, 0x41) - Ctrl+A."""
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def focus(hwnd):
    """Показать окно (из трея / свёрнутого), вывести на передний план."""
    if not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    if user32.GetForegroundWindow() != hwnd:
        # Windows не даёт перехватить фокус у чужого процесса, пока мы не
        # присоединимся к его потоку ввода. Alt для этого нельзя: он открывает меню.
        fg = user32.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        user32.AttachThreadInput(cur, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(cur, fg_tid, False)
    time.sleep(0.3)

def cursor():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


_tls = threading.local()


def _uia():
    """IUIAutomation: свой экземпляр в каждом потоке (COM нельзя передавать между потоками)."""
    ia = getattr(_tls, "ia", None)
    if ia is None:
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        ia = (comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA)
        _tls.ia = ia
    return ia

def render_widget(top):
    """Основной экран Chromium: самое большое дочернее окно Chrome_RenderWidgetHostHWND (видимое важнее).

    Если открыта встроенная панель браузера, экранов несколько (лишние скрыты),
    поэтому первое попавшееся брать нельзя.
    """
    res = []
    Proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        b = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, b, 128)
        if b.value == "Chrome_RenderWidgetHostHWND":
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            # сначала видимые, потом по площади; после перезапуска Claude основной экран бывает помечен
            # как «невидимый», тогда берём самый большой из имеющихся
            res.append((bool(user32.IsWindowVisible(h)), (r.right - r.left) * (r.bottom - r.top), h))
        return True

    user32.EnumChildWindows(top, Proc(cb), 0)
    return max(res)[2] if res else None

_last_mic = None  # (прямоугольник окна, точка кнопки) с прошлого удачного поиска
# Claude умеет делить экран на панели (сессия и тред рядом), у каждой своё поле ввода и своя кнопка диктовки.
# Здесь (левый, правый) край поля той панели, куда идёт диктовка: чтение текста, Enter и «Остановить» идут туда же.
_pane = None


def _window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def _prompts(ia, UIA, root):
    """Поля ввода Claude («Промпт») с прямоугольниками: [(элемент, (л, в, п, н))]. Видимые, а если таких нет - все."""
    cond = ia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId)
    arr = root.FindAll(UIA.TreeScope_Descendants, cond)
    shown, hidden = [], []
    for i in range(arr.Length):
        try:
            e = arr.GetElement(i)
            if (e.CurrentName or "").strip().lower() not in ("промпт", "prompt"):
                continue
            r = e.CurrentBoundingRectangle
            (hidden if e.CurrentIsOffscreen else shown).append((e, (r.left, r.top, r.right, r.bottom)))
        except comtypes.COMError:
            pass   # элемент пропал во время перерисовки
    return shown or hidden


def _x_gap(x, rc):
    """На сколько пикселей точка левее или правее прямоугольника (0 - она над ним)."""
    return max(rc[0] - x, 0, x - rc[2])


def _pick_prompt(prompts):
    """Поле панели, куда диктуем. Если панелей несколько: та, что запомнена для текущей записи;
    иначе та, над которой стоит курсор (пультом показали, куда диктовать). Курсор над боковым меню
    или между панелями - ближайшая по горизонтали."""
    if len(prompts) <= 1:
        return prompts[0] if prompts else None
    if _pane:
        mid = (_pane[0] + _pane[1]) // 2
        for p in prompts:
            if p[1][0] <= mid <= p[1][2]:
                return p
    x = cursor()[0]
    return min(prompts, key=lambda p: _x_gap(x, p[1]))


def find_mic(hwnd, pattern, tries=12):
    """Центр кнопки диктовки в окне Claude (физические пиксели) или None.

    Дерево доступности Chromium бывает недоступно на мгновения: после
    восстановления окна и пока Claude перерисовывает ответ. Поэтому много
    коротких попыток. Старую позицию не подставляем: раскладка внутри окна могла измениться.
    Если экран разделён на панели, берём кнопку панели из _pick_prompt и запоминаем эту панель в _pane;
    кнопку чужой панели не берём никогда (лучше ещё одна попытка, чем диктовка не туда).
    """
    global _last_mic, _pane
    ia, UIA = _uia()
    pat = re.compile(pattern, re.I)
    for _ in range(tries):
        try:
            rw = render_widget(hwnd)
            if rw:
                root = ia.ElementFromHandle(rw)
                cond = ia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId)
                arr = root.FindAll(UIA.TreeScope_Descendants, cond)
                found = []
                for i in range(arr.Length):
                    try:
                        e = arr.GetElement(i)
                        if e.CurrentIsOffscreen or not pat.search(e.CurrentName or ""):
                            continue
                        r = e.CurrentBoundingRectangle
                    except comtypes.COMError:
                        continue  # элемент пропал во время перерисовки
                    if r.right > r.left and r.bottom > r.top:
                        found.append(((r.left + r.right) // 2, (r.top + r.bottom) // 2))
                pt = None
                prompts = _prompts(ia, UIA, root) if found else []
                if len(prompts) > 1:
                    target = _pick_prompt(prompts)[1]
                    _pane = (target[0], target[2])
                    # кнопка панели стоит под её полем ввода или в нём, по горизонтали в пределах поля
                    mine = [p for p in found if _x_gap(p[0], target) <= 40]
                    if mine:
                        pt = min(mine, key=lambda p: abs(p[1] - (target[1] + target[3]) // 2))
                elif found:
                    pt = found[0]
                if pt:
                    _last_mic = (_window_rect(hwnd), pt)
                    return pt
        except comtypes.COMError:
            pass
        time.sleep(0.25)
    return None

def read_prompt(hwnd):
    """(текст, в_фокусе) поля ввода Claude («Промпт») или None, если не удалось прочитать."""
    try:
        ia, UIA = _uia()
        p = _pick_prompt(_prompts(ia, UIA, ia.ElementFromHandle(render_widget(hwnd))))
        if p:
            vp = p[0].GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
            return vp.CurrentValue or "", bool(p[0].CurrentHasKeyboardFocus)
    except Exception:
        pass
    return None


def describe_bottom(hwnd, limit=14):
    """Подписи кнопок и полей у нижнего края окна: показывает, какой экран Claude открыт, когда микрофон не найден."""
    try:
        ia, UIA = _uia()
        rw = render_widget(hwnd)
        if not rw:
            return "основной экран Chromium не найден"
        root = ia.ElementFromHandle(rw)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        arr = root.FindAll(UIA.TreeScope_Descendants, ia.CreateTrueCondition())
        names = []
        for i in range(arr.Length):
            try:
                e = arr.GetElement(i)
                bb = e.CurrentBoundingRectangle
                if e.CurrentIsOffscreen or bb.top < r.bottom - 260:
                    continue
                nm = (e.CurrentName or "").strip()
                if nm and e.CurrentLocalizedControlType in ("кнопка", "поле", "кнопка-переключатель", "button", "edit"):
                    names.append(nm[:28])
            except Exception:
                pass
        return "у нижнего края: " + (", ".join(dict.fromkeys(names)[:limit] if False else list(dict.fromkeys(names))[:limit]) or "ничего")
    except Exception as e:
        return "не удалось прочитать интерфейс: %r" % (e,)


def focus_prompt(hwnd):
    """Поставить фокус клавиатуры в поле ввода («Промпт») нашей панели. Нужен, если фокус остался
    на кнопке микрофона или в другой панели."""
    try:
        ia, UIA = _uia()
        p = _pick_prompt(_prompts(ia, UIA, ia.ElementFromHandle(render_widget(hwnd))))
        if p:
            p[0].SetFocus()
            time.sleep(0.15)
            return True
    except Exception:
        pass
    return False


def prompt_rects(hwnd):
    """Прямоугольники всех полей ввода Claude («Промпт») в физических пикселях (в разделённом виде их несколько)."""
    try:
        ia, UIA = _uia()
        return [p[1] for p in _prompts(ia, UIA, ia.ElementFromHandle(render_widget(hwnd)))]
    except Exception:
        return []


class PromptHover:
    """Следит в фоне, стоит ли курсор над строкой ввода Claude (и окно Claude активно).
    Хук клавиатуры должен отвечать мгновенно, поэтому тяжёлую работу с UI Automation делаем здесь."""
    MARGIN = 40   # пикселей вокруг поля: чуть шире самого поля

    def __init__(self):
        self.inside = False
        self.text_focus = False   # в любом окне фокус стоит в поле ввода (для «Системного набирателя»)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            tf = False
            try:
                ia, UIA = _uia()
                fe = ia.GetFocusedElement()
                tf = fe.CurrentControlType in (UIA.UIA_EditControlTypeId, UIA.UIA_ComboBoxControlTypeId)
            except Exception:
                tf = False
            self.text_focus = tf
            inside = False
            try:
                hwnd = claude_window()
                if hwnd and user32.GetForegroundWindow() == hwnd:
                    x, y = cursor()
                    m = self.MARGIN
                    inside = any(rc[0] - m <= x <= rc[2] + m and rc[1] - m <= y <= rc[3] + m
                                 for rc in prompt_rects(hwnd))
            except Exception:
                inside = False
            self.inside = inside
            time.sleep(0.3)


def wait_for_text(hwnd, stable, max_wait, cancelled=lambda: False):
    """Ждём, пока Claude дораспознает текст: он непустой и не менялся `stable` секунд."""
    t0 = last_change = time.time()
    last = None
    while time.time() - t0 < max_wait and not cancelled():
        cur = read_prompt(hwnd)
        if cur is None:
            time.sleep(0.15)
            continue
        text = cur[0].strip()
        if text != last:
            last, last_change = text, time.time()
        elif text and time.time() - last_change >= stable:
            return True
        time.sleep(0.15)
    return False

def find_audio(name):
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower() \
                and sd.query_hostapis(d["hostapi"])["name"] == "MME":
            return i
    return None


class ClickWatcher:
    """Низкоуровневый хук мыши: замечает НАСТОЯЩИЙ клик пользователя (не наш эмулированный)."""
    WH_MOUSE_LL, WM_LBUTTONDOWN, LLMHF_INJECTED = 14, 0x0201, 0x01

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    def __init__(self, remote_tag=None):
        self.event = threading.Event()
        self.key_rules = {}  # vk -> (условие, действие): клавиша «проглатывается», если условие истинно
        self.enabled = True   # False: все подмены клавиш выключены, клавиши идут как обычно
        self.hold_keys = {}  # vk -> (при_нажатии, при_отпускании): клавиша целиком подменяется (например, Menu -> правая кнопка)
        self.remote_tag = remote_tag      # например "vid_4842&pid_0001"; None = реагировать на любую клавиатуру
        self.raw = collections.deque(maxlen=64)   # [время, vk, с_пульта?, использовано?]
        self.raw_ok = False
        self.pending = None      # (vk, время, действие): ждём из Raw Input, с пульта ли клавиша
        self._last_open = 0.0
        self._dev_cache = {}
        self.dev_names = {}   # hDevice -> имя устройства (для мастера настройки)
        threading.Thread(target=self._loop, daemon=True).start()
        # Raw Input обрабатывается в ОТДЕЛЬНОМ потоке: поток хука, пока ждёт ответа, не может разбирать сообщения
        threading.Thread(target=self._raw_loop, daemon=True).start()

    # --- Raw Input: с какого устройства пришла клавиша (обычный хук этого не знает) ---
    class _RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", ctypes.c_void_p), ("wParam", wintypes.WPARAM)]

    class _RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                    ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]

    def _raw_loop(self):
        self._setup_raw()   # окно создаётся в ЭТОМ потоке
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x00FF:   # WM_INPUT
                self._on_raw(msg.lParam)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _setup_raw(self):
        try:
            class RID(ctypes.Structure):
                _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                            ("dwFlags", wintypes.DWORD), ("hwndTarget", ctypes.c_void_p)]
            user32.CreateWindowExW.restype = ctypes.c_void_p
            user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            hwnd = user32.CreateWindowExW(0, "STATIC", "AirDictateRaw", 0, 0, 0, 0, 0,
                                          ctypes.c_void_p(-3), None, None, None)   # HWND_MESSAGE
            rid = RID(1, 6, 0x100, hwnd)   # клавиатуры, RIDEV_INPUTSINK: получаем и при чужом фокусе
            user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RID), wintypes.UINT, wintypes.UINT]
            self.raw_ok = bool(hwnd) and bool(user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RID)))
        except Exception as e:
            log("Raw Input недоступен:", repr(e))
            self.raw_ok = False
        if not self.raw_ok:
            log("не удалось определить устройство клавиш: стрелки будут работать с любой клавиатуры")

    def _is_remote(self, hdev):
        if hdev in self._dev_cache:
            return self._dev_cache[hdev]
        size = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                                  ctypes.POINTER(wintypes.UINT)]
        user32.GetRawInputDeviceInfoW(hdev, 0x20000007, None, ctypes.byref(size))   # RIDI_DEVICENAME
        buf = ctypes.create_unicode_buffer(max(1, size.value))
        user32.GetRawInputDeviceInfoW(hdev, 0x20000007, buf, ctypes.byref(size))
        res = bool(self.remote_tag) and self.remote_tag in buf.value.lower()
        self.dev_names[hdev] = buf.value
        self._dev_cache[hdev] = res
        return res

    def _on_raw(self, lparam):
        try:
            hdr = ctypes.sizeof(self._RAWINPUTHEADER)
            size = wintypes.UINT(0)
            user32.GetRawInputData.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.UINT), wintypes.UINT]
            user32.GetRawInputData(lparam, 0x10000003, None, ctypes.byref(size), hdr)   # RID_INPUT
            buf = ctypes.create_string_buffer(size.value)
            if user32.GetRawInputData(lparam, 0x10000003, buf, ctypes.byref(size), hdr) != size.value:
                return
            header = self._RAWINPUTHEADER.from_buffer(buf)
            if header.dwType != 1:   # не клавиатура
                return
            kb = self._RAWKEYBOARD.from_buffer(buf, hdr)
            if kb.Flags & 1:         # отпускание клавиши
                return
            remote = self._is_remote(header.hDevice)
            self.raw.append([time.time(), kb.VKey, remote, False, self.dev_names.get(header.hDevice, "")])
            p = self.pending
            if p and p[0] == kb.VKey and time.time() - p[1] < 0.3:
                self.pending = None
                if remote and time.time() - self._last_open > 0.5:   # не чаще раза в полсекунды (автоповтор)
                    self._last_open = time.time()
                    threading.Thread(target=p[2], daemon=True).start()
        except Exception:
            pass

    _dbg_last = 0.0

    def _dbg(self, text):
        """Диагностика стрелок в журнал, не чаще раза в 0,4 с (автоповтор клавиши)."""
        now = time.time()
        if now - self._dbg_last > 0.4:
            self._dbg_last = now
            log(text)

    def from_remote(self, vk, wait=0.03):
        """Пришла ли клавиша vk с пульта? Raw Input и хук приходят независимо, поэтому ждём пару миллисекунд.
        Нет сведений (Raw Input не поднялся) - считаем, что с любой клавиатуры, как раньше."""
        if not self.remote_tag or not self.raw_ok:
            return True
        end = time.time() + wait
        while True:
            now = time.time()
            try:
                items = list(self.raw)
            except RuntimeError:
                items = []
            for it in reversed(items):
                if it[1] == vk and not it[3] and now - it[0] < 0.25:
                    it[3] = True
                    return bool(it[2])
            if now >= end:
                return False   # события от устройства нет: не пульт (например, эмуляция), пропускаем клавишу
            time.sleep(0.002)

    def _loop(self):
        Proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        def cb(code, wparam, lparam):
            if code >= 0 and wparam == self.WM_LBUTTONDOWN:
                info = ctypes.cast(lparam, ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
                if not info.flags & self.LLMHF_INJECTED:
                    self.event.set()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._proc = Proc(cb)  # держим ссылку, иначе соберёт сборщик мусора
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, Proc, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._hook = user32.SetWindowsHookExW(self.WH_MOUSE_LL, self._proc, None, 0)

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]
        swallowed = set()
        held = set()

        def kcb(code, wparam, lparam):
            if code >= 0 and self.enabled:
                info = ctypes.cast(lparam, ctypes.POINTER(KBD)).contents
                vk = info.vkCode
                down = wparam in (0x100, 0x104)
                if not info.flags & 0x10:  # не наша эмуляция
                    if not down and vk in swallowed:
                        swallowed.discard(vk)
                        return 1
                    hk = self.hold_keys.get(vk)
                    if hk:
                        if down:
                            if vk not in held:   # автоповтор не повторяет нажатие
                                held.add(vk)
                                threading.Thread(target=hk[0], daemon=True).start()
                        elif vk in held:
                            held.discard(vk)
                            threading.Thread(target=hk[1], daemon=True).start()
                        return 1
                    rule = self.key_rules.get(vk)
                    if down and rule:
                        pred, action, *rest = rule
                        detect = rest[0] if rest else None   # требуется ли узнавать источник (панель ещё закрыта)
                        try:
                            ok = pred()
                        except Exception as e:
                            ok = False
                            self._dbg("ошибка условия для клавиши 0x%02X: %r" % (vk, e))
                        cond = ok
                        if ok and detect is not None and self.remote_tag and self.raw_ok:
                            try:
                                need = detect()
                            except Exception:
                                need = False
                            if need:
                                # Windows присылает Raw Input только ПОСЛЕ хука и только если клавиша пропущена,
                                # поэтому клавишу пропускаем, а действие запустит _on_raw, если она пришла с пульта
                                self.pending = (vk, time.time(), action)
                                ok = False
                        self._dbg("стрелка 0x%02X: условие=%s -> %s" % (
                            vk, cond, "перехват" if ok else ("жду источник из Raw Input" if self.pending and self.pending[0] == vk and cond else "пропускаю")))
                        if ok:
                            swallowed.add(vk)
                            threading.Thread(target=action, daemon=True).start()
                            return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._kproc = Proc(kcb)
        self._khook = user32.SetWindowsHookExW(13, self._kproc, None, 0)  # WH_KEYBOARD_LL
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

class Highlight:
    """Пульсирующее кольцо вокруг курсора, пока он заперт на кнопке: «я не завис, я жму кнопку».

    Окно полностью прозрачно для мыши (WS_EX_TRANSPARENT) и не берёт фокус, поэтому наш же
    клик проходит сквозь него.
    """
    SIZE = 150
    COLOR = "#ff3b30"

    def __init__(self):
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, x, y):
        self.q.put(("show", x, y))

    def hide(self):
        self.q.put(("hide",))

    def _run(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            key = "#010101"
            win.configure(bg=key)
            win.attributes("-transparentcolor", key)
            S = self.SIZE
            cv = tk.Canvas(win, width=S, height=S, bg=key, highlightthickness=0)
            cv.pack()
            win.update_idletasks()
            top = user32.GetParent(win.winfo_id()) or win.winfo_id()
            # LAYERED | TRANSPARENT (клики насквозь) | NOACTIVATE | TOOLWINDOW
            user32.SetWindowLongW(top, -20, user32.GetWindowLongW(top, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80)
            win.withdraw()
        except Exception as e:
            log("подсветка недоступна:", repr(e))
            return

        st = {"on": False, "t0": 0.0}
        c = S // 2

        def frame():
            while True:
                try:
                    cmd = self.q.get_nowait()
                except queue.Empty:
                    break
                if cmd[0] == "show":
                    win.geometry("%dx%d+%d+%d" % (S, S, cmd[1] - c, cmd[2] - c))
                    win.deiconify()
                    win.attributes("-topmost", True)
                    st["on"], st["t0"] = True, time.time()
                else:
                    st["on"] = False
                    win.withdraw()
            if st["on"]:
                phase = ((time.time() - st["t0"]) % 0.7) / 0.7
                cv.delete("all")
                r = 18 + phase * 50
                cv.create_oval(c - r, c - r, c + r, c + r, outline=self.COLOR, width=max(1, int(7 * (1 - phase))))
                cv.create_oval(c - 20, c - 20, c + 20, c + 20, outline=self.COLOR, width=3)
                cv.create_oval(c - 5, c - 5, c + 5, c + 5, fill=self.COLOR, outline=self.COLOR)
            root.after(16, frame)

        frame()
        root.mainloop()

NUMPAD_THEMES = {
    # claude-dark подобран по реальным цветам тёмного Claude: фон #151515, плашки #2d2d2d, текст #f0efec, акцент #d97757
    "claude-dark": dict(base="#1c1c1b", rim="#3b3b39", ring="#2d2d2c", center="#383836",
                        hover="#d97757", press="#c6613f", text="#f0efec", accent="#d97757"),
    "claude-light": dict(base="#f5f4ee", rim="#d6d3c6", ring="#e8e5d9", center="#dfdbcc",
                         hover="#d97757", press="#c6613f", text="#2b2a27", accent="#c6613f"),
    "sky": dict(base="#171a1f", rim="#343b46", ring="#252a32", center="#2f3640",
                hover="#6a9bcc", press="#4f81b3", text="#eef2f7", accent="#6a9bcc"),
    "olive": dict(base="#1a1c17", rim="#3a3f33", ring="#272a22", center="#32362b",
                  hover="#788c5d", press="#5f7247", text="#f0efe6", accent="#9bb27a"),
}


class Numpad:
    """Круговая клавиатура для цифр: кольцо из сегментов-клавиш (+, 1…9, 0 по часовой, плюс сверху),
    в центре Enter, точка, запятая и частые знаки (пробел и удаление на стрелках пульта). Появляется центром под курсором,
    двигается за тёмный фон. Окно не забирает фокус: символы идут в то же поле ввода Claude."""

    KEYS = ["+", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]

    def __init__(self, theme="claude-dark"):
        self.theme = NUMPAD_THEMES.get(theme) or NUMPAD_THEMES["claude-dark"]
        self.q = queue.Queue()
        self.visible = False
        self.pos = None  # где пользователь оставил панель: при следующем показе она появится там же
        threading.Thread(target=self._run, daemon=True).start()

    def toggle(self, x, y):
        self.q.put(("toggle", x, y))

    def hide(self):
        self.q.put(("hide",))

    @staticmethod
    def send_char(ch):
        """Печать символа независимо от раскладки (SendInput + KEYEVENTF_UNICODE)."""
        class KI(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

        class MI(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

        class U(ctypes.Union):
            _fields_ = [("ki", KI), ("mi", MI)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", U)]

        items = (INPUT * 2)()
        for i, flags in enumerate((0x0004, 0x0004 | 0x0002)):  # UNICODE, UNICODE|KEYUP
            items[i].type = 1
            items[i].ki = KI(0, ord(ch), flags, 0, None)
        user32.SendInput(2, ctypes.byref(items), ctypes.sizeof(INPUT))

    @staticmethod
    def backspace():
        user32.keybd_event(0x08, 0, 0, 0)
        user32.keybd_event(0x08, 0, KEYEVENTF_KEYUP, 0)

    @staticmethod
    def enter():
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

    def _run(self):
        import math
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            key = "#010101"
            win.configure(bg=key)
            win.attributes("-transparentcolor", key)   # вне фигуры окно прозрачно, клики проходят насквозь

            try:
                scale = max(1.0, user32.GetDpiForSystem() / 96.0)
            except Exception:
                scale = 1.0
            D = int(210 * scale)            # диаметр диска, пиксели
            S = int(D * 1.04)               # окно чуть больше диска (место под кромку)
            c = S / 2
            cv = tk.Canvas(win, width=S, height=S, bg=key, highlightthickness=0)
            cv.pack()

            # палитра из выбранной темы
            th = self.theme
            BASE, RING, CENTER, RIM = th["base"], th["ring"], th["center"], th["rim"]
            HOVER, PRESS, TEXT, ACCENT = th["hover"], th["press"], th["text"], th["accent"]
            fdig = -max(14, int(0.082 * D))     # шрифты в пикселях (минус), иначе Tk масштабирует их повторно
            fsm = -max(11, int(0.05 * D))
            INS = 0.016 * D                     # скругление углов клавиш (радиус)

            # ободок (за него тянут панель) и основание внутри него
            cv.create_oval(c - D / 2, c - D / 2, c + D / 2, c + D / 2, fill=RIM, outline="", tags=("base",))
            rim_w = 0.034 * D
            base = cv.create_oval(c - D / 2 + rim_w, c - D / 2 + rim_w, c + D / 2 - rim_w, c + D / 2 - rim_w,
                                  fill=BASE, outline="", tags=("base",))

            def poly(tag, pts, label, lx, ly, action, bg, fs, fg=TEXT, release=None):
                cv.create_polygon(pts, fill=bg, outline=bg, width=2 * INS, joinstyle="round", tags=(tag, tag + "bg"))
                cv.create_text(lx, ly, text=label, fill=fg, font=("Segoe UI Semibold", fs), tags=(tag,))

                def paint(color):
                    for it in cv.find_withtag(tag + "bg"):
                        cv.itemconfigure(it, fill=color, outline=color)
                cv.tag_bind(tag, "<Enter>", lambda e: paint(HOVER))
                cv.tag_bind(tag, "<Leave>", lambda e: paint(bg))
                cv.tag_bind(tag, "<ButtonPress-1>", lambda e: (paint(PRESS), action()))
                cv.tag_bind(tag, "<ButtonRelease-1>", lambda e: (paint(HOVER), release() if release else None))

            def sector(a0, a1, r_in, r_out, steps=14):
                """Сегмент кольца как многоугольник."""
                pts = []
                for i in range(steps + 1):
                    a = math.radians(a0 + (a1 - a0) * i / steps)
                    pts += [c + r_out * math.cos(a), c + r_out * math.sin(a)]
                for i in range(steps, -1, -1):
                    a = math.radians(a0 + (a1 - a0) * i / steps)
                    pts += [c + r_in * math.cos(a), c + r_in * math.sin(a)]
                return pts

            def rrect(x0, y0, x1, y1, r):
                """Скруглённый прямоугольник как многоугольник со сглаживанием."""
                return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
                        x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

            # --- кольцо клавиш: плюс сверху, дальше по часовой 1…9, 0 ---
            n = len(self.KEYS)
            step = 360.0 / n
            gap = 1.0
            r_out, r_in = 0.462 * D, 0.30 * D
            for i, ch in enumerate(self.KEYS):
                mid = -90 + i * step
                tag = "k%d" % i
                r_mid = (r_in + r_out) / 2
                da = math.degrees(INS / r_mid)
                pts = sector(mid - step / 2 + gap + da, mid + step / 2 - gap - da, r_in + INS, r_out - INS)
                lr = (r_in + r_out) / 2
                lx, ly = c + lr * math.cos(math.radians(mid)), c + lr * math.sin(math.radians(mid))
                poly(tag, pts, ch, lx, ly, (lambda ch=ch: Numpad.send_char(ch)), RING, fdig,
                     fg=ACCENT if ch == "+" else TEXT)

            # --- центр: диск, разделённый на сегменты (как кольцо), каждая клавиша обрезана по окружности ---
            Rc = 0.285 * D   # радиус заполняемой центральной части

            def cell(x0, x1, y0, y1, n=18):
                """Прямоугольник (в долях D от центра), обрезанный окружностью Rc: контур точками, лишние
                точки проецируются на окружность."""
                x0, x1, y0, y1 = (v * D for v in (x0, x1, y0, y1))
                x0, x1, y0, y1 = x0 + INS, x1 - INS, y0 + INS, y1 - INS
                edge = []
                for i in range(n + 1):
                    edge.append((x0 + (x1 - x0) * i / n, y0))
                for i in range(1, n + 1):
                    edge.append((x1, y0 + (y1 - y0) * i / n))
                for i in range(1, n + 1):
                    edge.append((x1 - (x1 - x0) * i / n, y1))
                for i in range(1, n):
                    edge.append((x0, y1 - (y1 - y0) * i / n))
                pts = []
                for px, py in edge:
                    r = math.hypot(px, py)
                    if r > Rc - INS:
                        px, py = px * (Rc - INS) / r, py * (Rc - INS) / r
                    pts += [c + px, c + py]
                return pts

            fpt = -int(0.14 * D)   # крупный шрифт для «.» и «,»

            HOLD_MS = 450

            def hold_key(tap, alt):
                """Клавиша с двумя символами: сразу печатается `tap`; если удерживать, он стирается и печатается `alt`.
                Возвращает (действие_при_нажатии, действие_при_отпускании) с общим состоянием."""
                state = {"job": None}

                def press():
                    Numpad.send_char(tap)
                    state["job"] = win.after(HOLD_MS, switch)

                def switch():
                    state["job"] = None
                    Numpad.backspace()
                    Numpad.send_char(alt)

                def release():
                    if state["job"] is not None:
                        win.after_cancel(state["job"])
                        state["job"] = None
                return press, release

            k_paren, k_dot, k_semi = hold_key("(", ")"), hold_key(",", "."), hold_key(";", ":")

            # три ряда: % − = / × / / , .   (Enter, пробел и Backspace живут на стрелках пульта)
            rows = [
                (-0.285, -0.0975, 0.040, [
                    (-0.285, -0.1025, "%", (lambda: Numpad.send_char("%")), fdig),
                    (-0.0875, 0.0875, "\u2212", (lambda: Numpad.send_char("-")), fdig),
                    (0.1025, 0.285, "=", (lambda: Numpad.send_char("=")), fdig)]),
                (-0.0825, 0.0825, 0.0, [
                    (-0.285, -0.0075, "\u00d7", (lambda: Numpad.send_char("\u00d7")), fdig),
                    (0.0075, 0.285, "/", (lambda: Numpad.send_char("/")), fdig)]),
                (0.0975, 0.285, -0.040, [
                    (-0.285, -0.1025, "(", k_paren[0], fdig, k_paren[1], ")"),
                    (-0.0875, 0.0875, ",", k_dot[0], fpt, k_dot[1], "."),
                    (0.1025, 0.285, ";", k_semi[0], fdig, k_semi[1], ":")]),
            ]
            for ri, (y0, y1, dy, cells) in enumerate(rows):
                for ci, (x0, x1, label, action, fs, *extra) in enumerate(cells):
                    yy = (y0 + y1) / 2 + dy                       # высота подписи (в долях D от центра)
                    hc = math.sqrt(max(0.0, 0.285 ** 2 - yy ** 2))  # полуширина диска на этой высоте
                    vx0, vx1 = max(x0, -hc), min(x1, hc)           # видимая часть клавиши
                    lx = c + (vx0 + vx1) / 2 * D
                    ly = c + yy * D - (0.03 * D if label in (".", ",") else 0)
                    tag = "c%d%d" % (ri, ci)
                    has_hint = len(extra) > 1
                    poly(tag, cell(x0, x1, y0, y1), label, lx - (0.02 * D if has_hint else 0), ly, action, CENTER, fs,
                         release=extra[0] if extra else None)
                    if has_hint:   # маленькая подсказка справа: что получится при удержании
                        cv.create_text(lx + 0.038 * D, ly + 0.012 * D, text=extra[1], fill=ACCENT,
                                       font=("Segoe UI Semibold", -max(11, int(0.052 * D))), tags=(tag,))

            # перетаскивание: тянуть за тёмный фон диска (просветы между клавишами и центр)
            drag = {"dx": 0, "dy": 0}

            def drag_start(e):
                drag["dx"], drag["dy"] = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()

            def drag_move(e):
                win.geometry("+%d+%d" % (e.x_root - drag["dx"], e.y_root - drag["dy"]))

            def drag_end(e):
                self.pos = (win.winfo_x(), win.winfo_y())

            cv.tag_bind("base", "<Enter>", lambda e: cv.configure(cursor="fleur"))
            cv.tag_bind("base", "<Leave>", lambda e: cv.configure(cursor=""))
            cv.tag_bind("base", "<ButtonPress-1>", drag_start)
            cv.tag_bind("base", "<B1-Motion>", drag_move)
            cv.tag_bind("base", "<ButtonRelease-1>", drag_end)

            win.update_idletasks()
            top = user32.GetParent(win.winfo_id()) or win.winfo_id()
            # NOACTIVATE | TOOLWINDOW: не забираем фокус у поля ввода
            user32.SetWindowLongW(top, -20, user32.GetWindowLongW(top, -20) | 0x08000000 | 0x80)
            win.withdraw()
        except Exception as e:
            log("цифровая панель недоступна:", repr(e))
            return

        def poll():
            while True:
                try:
                    cmd = self.q.get_nowait()
                except queue.Empty:
                    break
                if cmd[0] == "hide" or (cmd[0] == "toggle" and self.visible):
                    win.withdraw()
                    self.visible = False
                else:
                    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
                    if self.pos:   # пользователь уже двигал панель: возвращаем на то же место
                        x, y = self.pos
                    else:          # иначе центром под курсор
                        x, y = cmd[1] - c, cmd[2] - c
                    x = int(min(max(0, x), sw - S))
                    y = int(min(max(0, y), sh - S))
                    win.geometry("+%d+%d" % (x, y))
                    win.deiconify()
                    win.attributes("-topmost", True)
                    self.visible = True
            root.after(30, poll)

        poll()
        root.mainloop()


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.active = False
        self.lock = threading.Lock()
        self.op_lock = threading.RLock()   # начало, остановка и пауза по клику никогда не идут одновременно
        self.gen = 0                       # номер записи: наблюдатель чужой записи ничего не трогает
        self.hb = 0.0                      # «пульс» наблюдателя текущей записи
        self.rec_started = 0.0
        self.last_send = False             # последняя остановка предполагала отправку
        self.left_is_down = False          # мы сейчас держим левую кнопку мыши
        self.down_at = 0.0                 # когда кнопка мыши реально была зажата
        self.stop_evt = threading.Event()
        self.saved_cursor = None
        self.stopped_at = 0.0
        self.speech_end = None   # когда вы замолчали (если запись остановила тишина): для журнала задержки отправки
        self.mic_pt = None
        self.unlock_timer = None
        self.unlocked = False
        tag = ("vid_%04x&pid_%04x" % (cfg["vid"], cfg["pid"])) if cfg.get("remote_only_keys", True) else None
        self.clicks = ClickWatcher(remote_tag=tag)   # стрелки пульта перехватываем только с него
        self.highlight = Highlight()  # показывается, только если в настройках highlight=true
        self.numpad = Numpad(cfg.get("numpad_theme", "claude-dark"))
        self.hover = PromptHover()  # курсор над строкой ввода: можно печатать цифры с пульта
        if cfg.get("menu_right_click", True):
            # кнопка подсветки пульта шлёт клавишу «Меню» (0x5D): превращаем её в правую кнопку мыши в точке курсора
            self.clicks.hold_keys[0x5D] = (self._right_down, self._right_up)
        if cfg.get("numpad_key", True):
            # стрелка вправо в записи/на паузе показывает цифровую панель у курсора (вместо движения каретки)
            self.clicks.key_rules[0x27] = (self._numpad_wanted, lambda: self.numpad.toggle(*cursor()),
                                           lambda: not self.numpad.visible)
            # стрелка влево, пока панель открыта, стирает символ (удержание стирает подряд)
            self.clicks.key_rules[0x25] = (self._numpad_open, Numpad.backspace)
            # стрелка вниз, пока панель открыта, ставит пробел (удержание ставит подряд)
            self.clicks.key_rules[0x28] = (self._numpad_open, lambda: Numpad.send_char(" "))
        self.on_state = None  # callback(имя_состояния) для значка в трее
        self.right_is_down = False
        self.notify = None   # callback(текст): всплывающее уведомление из трея
        self.send_token = 0  # увеличивается при каждой отправке и каждой новой записи
        self.sent_texts = load_history()  # отправленные фразы (и между перезапусками): узнаём фразу, вызванную стрелкой вверх
        self.start_text = ""  # текст в поле на момент начала записи
        self.resume_pending = False  # True: диктовка на паузе, текст в поле - наш черновик, его не трогаем
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _right_down(self):
        self.right_is_down = True
        user32.mouse_event(0x0008, 0, 0, 0, 0)

    def _right_up(self):
        user32.mouse_event(0x0010, 0, 0, 0, 0)
        self.right_is_down = False

    def _numpad_open(self):
        """Стрелки влево (Backspace) и вниз (пробел) подменяем, пока видна круговая клавиатура."""
        return self.numpad.visible

    def _numpad_wanted(self):
        """Стрелку вправо «проглатываем» (открываем/закрываем клавиатуру):
        - панель уже открыта (закрыть можно из любого окна);
        - в Claude: идёт запись, диктовка на паузе или курсор над строкой ввода;
        - в любом другом окне («Системный набиратель»): фокус стоит в поле ввода."""
        if self.numpad.visible:
            return True
        hwnd = claude_window()
        if hwnd and user32.GetForegroundWindow() == hwnd:
            if self.active or self.resume_pending:
                return True
            return bool(self.cfg.get("hover_keyboard", True)) and self.hover.inside
        return bool(self.cfg.get("system_keyboard", True)) and self.hover.text_focus

    def _state(self, name):
        if self.on_state:
            try:
                self.on_state(name)
            except Exception:
                pass
    def _unlock(self):
        """Через несколько секунд отпускаем курсор: запись при этом продолжается."""
        if self.active:
            user32.ClipCursor(None)
            if self.highlight:
                self.highlight.hide()
            self.unlocked = True
            log("курсор свободен, запись продолжается")

    # --- действия мышью ---

    def start(self):
        with self.op_lock:
            self._start()

    def _start(self):
        global _pane
        hwnd = claude_window()
        if not hwnd:
            log("Окно Claude не найдено")
            return
        focus(hwnd)
        _pane = None   # панель выбираем заново при каждой записи: ту, над которой курсор
        self.speech_end = None
        pt = find_mic(hwnd, self.cfg["mic_button_pattern"], tries=36)   # до ~9 с: окно может «просыпаться»
        if pt and _pane:
            log("экран Claude разделён на панели: диктую в панель x=%d…%d" % _pane)
        if not pt:
            # в активном окне кнопки нет (другой экран приложения): пробуем остальные окна Claude
            for other in claude_windows():
                if other != hwnd:
                    pt2 = find_mic(other, self.cfg["mic_button_pattern"], tries=3)
                    if pt2:
                        log("в активном окне кнопки нет, беру другое окно Claude")
                        hwnd, pt = other, pt2
                        focus(hwnd)
                        pt = find_mic(hwnd, self.cfg["mic_button_pattern"], tries=3) or pt2
                        break
        if not pt and not find_mic(hwnd, STOP_PATTERN, tries=1):
            # после долгого простоя окно Chromium бывает «заснувшим»: сворачиваем, разворачиваем и снова
            # выводим на передний план, это заставляет его перерисоваться и отдать дерево элементов
            log("кнопка не найдена, бужу окно Claude (свернуть и развернуть)")
            user32.ShowWindow(hwnd, 6)    # SW_MINIMIZE
            time.sleep(0.4)
            user32.ShowWindow(hwnd, 9)    # SW_RESTORE
            time.sleep(0.6)
            focus(hwnd)
            pt = find_mic(hwnd, self.cfg["mic_button_pattern"], tries=24)
            if pt:
                log("после пробуждения окна кнопка найдена")
        if not pt:
            stop_pt = find_mic(hwnd, STOP_PATTERN, tries=2)
            if stop_pt:
                log("диктовка в Claude уже идёт: останавливаю кнопкой «Остановить диктовку»")
                old = cursor()
                user32.SetCursorPos(*stop_pt)
                time.sleep(0.08)
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                time.sleep(0.05)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                user32.SetCursorPos(*old)
                self.start_text = ""
                self.send_token += 1
                if self.cfg.get("press_enter", True):
                    threading.Thread(target=self._send_when_ready, args=(self.send_token,), daemon=True).start()
                self._state("idle")
                return
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            log("Кнопка диктовки не найдена. Окно: видимо=%s свёрнуто=%s размер=%sx%s. "
                "Откройте чат с полем ввода или увеличьте окно (в узком окне панель может прятать кнопку)."
                % (bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd)),
                   r.right - r.left, r.bottom - r.top))
            log(describe_bottom(hwnd))
            if self.notify:
                self.notify("Кнопка диктовки не найдена. Откройте чат с полем ввода в Claude.")
            return
        cur = read_prompt(hwnd)
        self.start_text = norm_text(cur[0]) if cur else ""
        if cur is None:
            log("не удалось прочитать поле ввода перед записью")
        elif not cur[1] and self.start_text and not self.start_text.startswith("/"):
            # в поле текст, но фокус не там (остался на кнопке микрофона): возвращаем фокус, иначе очистка не сработает
            if focus_prompt(hwnd):
                cur = read_prompt(hwnd) or cur
            if not cur[1]:
                log("в поле есть текст, но фокус не в поле ввода: не чищу")
        if cur and cur[1] and self.start_text and not self.start_text.startswith("/"):
            # команда (/...) - нормально, не трогаем; всё остальное, что не продолжение паузы, чистим
            from_history = self.start_text in self.sent_texts
            if from_history or not self.resume_pending:
                reason = "фраза из истории" if from_history else "не продолжение паузы"
                for attempt in range(1, 4):
                    press_keys(VK_CONTROL, 0x41)  # Ctrl+A
                    press_keys(0x2E)              # Delete
                    time.sleep(0.2)
                    now = read_prompt(hwnd)
                    if now is not None and not norm_text(now[0]):
                        log("в поле был посторонний текст (%s) - очистил" % reason)
                        break
                else:
                    log("не удалось очистить поле за 3 попытки - записываю поверх")
                self.start_text = ""
                self.resume_pending = False
        elif not self.start_text:
            self.resume_pending = False
        self.saved_cursor = cursor()
        # Аэромышь в руке гоняет курсор: запираем его над кнопкой на время записи,
        # иначе клик промахивается или «срывается» с кнопки.
        lock = wintypes.RECT(pt[0], pt[1], pt[0] + 1, pt[1] + 1)
        user32.ClipCursor(ctypes.byref(lock))
        for _ in range(10):
            user32.SetCursorPos(*pt)
            time.sleep(0.02)
            cx, cy = cursor()
            if abs(cx - pt[0]) <= 1 and abs(cy - pt[1]) <= 1:
                break
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.left_is_down = True
        self.down_at = time.time()
        self.mic_pt = pt
        if self.highlight and self.cfg.get("highlight", True):
            self.highlight.show(*pt)
        self.numpad.hide()
        self.clicks.event.clear()
        self.send_token += 1  # новая запись отменяет ещё не ушедшую отправку
        self.active = True
        secs = self.cfg["cursor_lock_seconds"]
        if secs and secs > 0:
            self.unlock_timer = threading.Timer(secs, self._unlock)
            self.unlock_timer.daemon = True
            self.unlock_timer.start()
        self.gen += 1
        self.stop_evt = threading.Event()
        self.rec_started = self.hb = time.time()
        threading.Thread(target=self._watch, args=(self.gen, self.stop_evt), daemon=True).start()
        log("● запись")
        self._state("recording")

    def stop(self, send=True):
        with self.op_lock:
            self._stop(send)

    def _stop(self, send=True):
        with self.lock:
            if not self.active:
                return
            self.active = False
        if self.unlock_timer:
            self.unlock_timer.cancel()
        if self.unlocked and self.mic_pt:
            # курсор уже свободен: вернуть на кнопку, чтобы отпускание попало по ней
            pt = self.mic_pt
            lock = wintypes.RECT(pt[0], pt[1], pt[0] + 1, pt[1] + 1)
            user32.ClipCursor(ctypes.byref(lock))
            user32.SetCursorPos(*pt)
            time.sleep(0.05)
        hold = self.cfg.get("min_hold_seconds", 0.8) - (time.time() - self.down_at)
        if hold > 0:   # слишком короткое нажатие Claude считает «тапом» и оставляет диктовку включённой
            time.sleep(hold)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.left_is_down = False
        user32.ClipCursor(None)  # отпускаем курсор
        if self.highlight:
            self.highlight.hide()
        self.unlocked = False
        self.stopped_at = time.time()
        self.last_send = bool(send and self.cfg["press_enter"])
        if self.saved_cursor:
            user32.SetCursorPos(*self.saved_cursor)
        self.stop_evt.set()
        log("■ стоп" + (", отправляю" if send and self.cfg["press_enter"] else ""))
        self._state("idle")
        if send and self.cfg["press_enter"]:
            self.send_token += 1
            threading.Thread(target=self._send_when_ready, args=(self.send_token,), daemon=True).start()

    def _send_when_ready(self, token):
        """Ждём, пока Claude дораспознает текст, и жмём Enter. Идёт в своём потоке, чтобы пульт
        отзывался сразу; новая запись (token изменился) отменяет отправку."""
        stopped, speech_end = self.stopped_at, self.speech_end
        self.speech_end = None
        try:
            hwnd = claude_window()
            if not hwnd:
                return
            ok = wait_for_text(hwnd, self.cfg["enter_stable_seconds"], self.cfg["enter_max_wait"],
                               lambda: token != self.send_token)
            if token != self.send_token:
                log("отправка отменена: вы продолжили запись")
                return
            focus(hwnd)
            cur = read_prompt(hwnd)
            if cur is not None and not cur[1] and focus_prompt(hwnd):
                cur = read_prompt(hwnd) or cur   # фокус ушёл (например, в другую панель Claude): возвращаем в наше поле
            if cur is not None and not cur[1]:
                log("фокус не в поле ввода - Enter не нажимаю")
                self.resume_pending = True  # текст остался как черновик
                return
            if not ok:
                log("текст не появился за отведённое время - Enter не нажимаю")
                return
            text = norm_text(cur[0]) if cur else ""
            if text == self.start_text:
                log("в поле ничего не добавилось - не отправляю")
                return
            # жмём Enter и проверяем, что сообщение правда ушло (поле опустело); иначе повторяем
            gone = False
            for attempt in (1, 2):
                entered = time.time()
                user32.keybd_event(VK_RETURN, 0, 0, 0)
                user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.8)
                after = read_prompt(hwnd)
                if after is None or norm_text(after[0]) != text:
                    gone = True
                    break
                if token != self.send_token:   # вы уже начали новую запись
                    return
                log("после Enter сообщение осталось в поле - повторяю (попытка %d)" % attempt)
            if not gone:
                # не ушло: оставляем как черновик, ничего не стираем и в «историю» не записываем
                log("сообщение не отправилось: оставляю в поле как черновик")
                self.resume_pending = True
                self._state("paused")
                return
            if speech_end:
                log("✓ отправлено через %.1f с после конца речи (тишина %.1f с + распознавание до Enter %.1f с)"
                    % (entered - speech_end, stopped - speech_end, entered - stopped))
            elif 0 < entered - stopped < 60:
                log("✓ отправлено через %.1f с после остановки (распознавание до Enter)" % (entered - stopped))
            else:
                log("✓ отправлено")
            self.sent_texts = (self.sent_texts + [text])[-HISTORY_LIMIT:]
            save_history(self.sent_texts)
            self.resume_pending = False
            self._state("sent")
        except Exception as e:
            log("ОШИБКА отправки:", repr(e))

    def abort_by_user_click(self):
        with self.op_lock:
            self._abort()

    def _abort(self):
        """Настоящий клик пользователя завершает диктовку в Claude; мы ничего не отправляем."""
        with self.lock:
            if not self.active:
                return
            self.active = False
        if self.unlock_timer:
            self.unlock_timer.cancel()
        user32.ClipCursor(None)
        if self.highlight:
            self.highlight.hide()
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)  # на случай, если наша «зажатая» кнопка осталась
        self.left_is_down = False
        self.unlocked = False
        self.stopped_at = time.time()
        self.last_send = False
        self.stop_evt.set()
        self.resume_pending = True  # текст остался как черновик
        log("вы кликнули сами - диктовка закончена, без отправки")
        self._state("paused")

    # --- автостоп по тишине ---
    def _watch(self, gen, evt):
        try:
            self._watch_inner(gen, evt)
        except Exception as e:
            log("ОШИБКА в наблюдателе:", repr(e))
            if self.gen == gen:
                self.stop(send=False)

    def _watch_inner(self, gen, evt):
        c = self.cfg
        dev = find_audio(c["audio_device"])
        if dev is None:
            # без микрофона пульта тишину не отследить: хотя бы не зависаем навсегда
            log("Микрофон пульта не найден, автостоп по тишине отключён")
            end = time.time() + c["max_seconds"]
            while self.active and self.gen == gen and not evt.is_set() and time.time() < end:
                self.hb = time.time()
                time.sleep(0.2)
            if self.active and self.gen == gen:
                self.stop(send=False)
            return
        log("слежу за звуком, устройство", dev)
        t0 = time.time()
        state = {"spoke": False, "quiet_since": None}

        def cb(indata, frames, t, status):
            rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            now = time.time()
            if rms > c["speech_on"]:
                state["spoke"] = True
            if rms >= c["silence_off"]:
                # любой звук выше шума прерывает отсчёт тишины
                state["quiet_since"] = None
            elif state["quiet_since"] is None:
                state["quiet_since"] = now

        with sd.InputStream(device=dev, channels=1, samplerate=44100, blocksize=2205, callback=cb):
            while self.active and self.gen == gen and not evt.is_set():
                time.sleep(0.1)
                now = time.time()
                self.hb = now
                if self.unlocked and self.clicks.event.is_set():
                    self.abort_by_user_click(); return
                if now - t0 > c["max_seconds"]:
                    self.stop(send=False); return
                if state["spoke"] and state["quiet_since"] and now - state["quiet_since"] > c["silence_seconds"]:
                    self.speech_end = state["quiet_since"]
                    log("тишина, останавливаю"); self.stop(); return
                if not state["spoke"] and now - t0 > c["no_speech_timeout"]:
                    log("речи не было, отмена"); self.stop(send=False); return

    def _watchdog(self):
        """Страховка: не даём записи «зависнуть» с зажатой кнопкой, как бы ни сложились нажатия."""
        released_since = 0.0
        stale = 0
        while True:
            time.sleep(0.5)
            try:
                now = time.time()
                if self.active:
                    released_since = 0.0
                    stale = 0
                    if now - self.rec_started > 3.0 and now - self.hb > 2.0:
                        log("сторож записи: наблюдатель не отвечает, останавливаю запись")
                        self.stop(send=False)
                    elif now - self.rec_started > self.cfg["max_seconds"] + 5:
                        log("сторож записи: слишком долгая запись, останавливаю")
                        self.stop(send=False)
                elif self.left_is_down:
                    # записи нет, а кнопка мыши нами зажата: отпускаем (с небольшой задержкой, вдруг идёт остановка)
                    released_since = released_since or now
                    if now - released_since > 1.0:
                        log("сторож записи: кнопка осталась зажатой без записи, отпускаю")
                        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                        user32.ClipCursor(None)
                        self.left_is_down = False
                        self.numpad.hide()
                        released_since = 0.0
                elif (1.2 < now - self.stopped_at < self.cfg.get("stale_watch_seconds", 4.0) and not self.resume_pending
                      and not (user32.GetAsyncKeyState(1) & 0x8000)):
                    # мы запись уже закончили, а в Claude диктовка всё ещё «слушает» (быстрые нажатия подряд):
                    # видим кнопку «Остановить диктовку» больше секунды - выключаем её
                    released_since = 0.0
                    hwnd = claude_window()
                    if hwnd and user32.GetForegroundWindow() == hwnd:
                        stale = stale + 1 if find_mic(hwnd, STOP_PATTERN, tries=1) else 0
                        if stale:
                            log("сторож записи: вижу «Остановить диктовку» (%d из 2)" % stale)
                        if stale >= 2:
                            stale = 0
                            self._stop_stale_dictation(hwnd)
                else:
                    released_since = 0.0
                    stale = 0
            except Exception as e:
                log("ошибка сторожа записи:", repr(e))

    def _stop_stale_dictation(self, hwnd):
        with self.op_lock:
            if self.active:
                return
            pt = find_mic(hwnd, STOP_PATTERN, tries=1)
            if not pt:
                return
            log("сторож записи: в Claude осталась включённая диктовка, выключаю")
            old = cursor()
            user32.SetCursorPos(*pt)
            time.sleep(0.08)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            user32.SetCursorPos(*old)
            self.stopped_at = time.time()   # дальше не дёргаем, пока снова не будет записи
            self.start_text = ""
            if self.last_send:
                self.send_token += 1
                threading.Thread(target=self._send_when_ready, args=(self.send_token,), daemon=True).start()


def self_cmd(*flags):
    """Команда, которой эта же программа запускает свои части: мастер (--wizard) или рабочий процесс (--app)."""
    if FROZEN:
        return [sys.executable, *flags]
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    if "--wizard" in flags:
        return [exe, os.path.join(HERE, "setup_wizard.py"), *[f for f in flags if f != "--wizard"]]
    return [exe, os.path.join(HERE, "voice_remote.py"), *flags]


def supervise():
    """Сторож собранной версии: держит рабочий процесс запущенным и поднимает его после сбоя.
    Штатная остановка (пункт «Выход» в меню значка) создаёт stopped.flag, и сторож тоже завершается."""
    flag = os.path.join(HERE, "stopped.flag")
    h = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\AirDictateSupervisor")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    try:
        os.remove(flag)          # вы сами запустили программу: прошлая остановка больше не действует
    except OSError:
        pass
    log("supervisor: started")
    fast = 0
    while not os.path.exists(flag):
        t0 = time.time()
        p = subprocess.Popen(self_cmd("--app"), creationflags=0x08000000)
        p.wait()
        if os.path.exists(flag):
            break
        fast = fast + 1 if time.time() - t0 < 8 else 0
        pause = 60 if fast >= 3 else 3
        log("supervisor: program exited, restarting in %d s" % pause)
        time.sleep(pause)
    log("supervisor: stopped by stopped.flag")


def self_check():
    """Самопроверка (AirDictate.exe --check): окно Claude и кнопка диктовки. Результат - в журнал."""
    log("самопроверка: начало")
    try:
        hwnd = claude_window()
        log("окно Claude:", hwnd)
        if hwnd:
            log("кнопка диктовки:", find_mic(hwnd, load_cfg()["mic_button_pattern"], tries=6))
    except Exception as e:
        log("самопроверка окна: ошибка", repr(e))
    log("самопроверка: конец")


WATCHDOG_TASK = "AirDictate watchdog"


def watchdog_command():
    """Что задача Планировщика запускает каждую минуту. Всегда через pythonw (без окна консоли)."""
    if FROZEN:
        return sys.executable, "--watchdog"
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return exe, '"%s" --watchdog' % os.path.abspath(__file__)


def ensure_watchdog():
    """Проверяет задачу «AirDictate watchdog» в Планировщике и создаёт (или исправляет путь), если её нет.
    Задача раз в минуту поднимает программу, если она выключилась сама, и не трогает её, если её выключили вручную."""
    import base64
    exe, args = watchdog_command()

    def q(s):   # строка для PowerShell в одинарных кавычках
        return s.replace("'", "''")
    ps = f"""
$name = '{q(WATCHDOG_TASK)}'
$exe = '{q(exe)}'
$wargs = '{q(args)}'
$t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($t -and $t.Actions[0].Execute -eq $exe -and $t.Actions[0].Arguments -eq $wargs) {{ 'ok' }} else {{
  $a = New-ScheduledTaskAction -Execute $exe -Argument $wargs
  $t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
  $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
  Register-ScheduledTask -TaskName $name -Action $a -Trigger @($t1,$t2) -Principal $p -Settings $s -Description 'Поднимает AirDictate, если программа не работает и её не останавливали вручную' -Force | Out-Null
  'created'
}}
"""
    try:
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        out = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                             capture_output=True, text=True, timeout=60, creationflags=0x08000000)
        last = (out.stdout or "").strip().splitlines()[-1:] or [""]
        if last[0] == "ok":
            log("сторож-задача AirDictate на месте")
        elif last[0] == "created":
            log("сторож-задача AirDictate создана: поднимет программу, если она выключится")
        else:
            log("сторож-задача не создана:", (out.stderr or out.stdout).strip()[:300])
    except Exception as e:
        log("сторож-задача: не удалось проверить:", repr(e))


def watchdog():
    """Задача Планировщика вызывает это каждую минуту. Если программу выключили вручную (stopped.flag), ничего не делаем.
    Иначе запускаем сторожа без окна; сторож сам выходит, если уже запущен (мьютекс)."""
    if os.path.exists(os.path.join(HERE, "stopped.flag")):
        return
    if FROZEN:
        cmd = self_cmd()
    else:
        cmd = ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
               "-File", os.path.join(HERE, "supervisor.ps1")]
    subprocess.Popen(cmd, creationflags=0x08000000)


def single_instance():
    """Вторая копия скрипта сразу выходит, иначе обе реагируют на каждое нажатие."""
    h = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\MyFlyMouseVoiceRemote")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit("Скрипт уже запущен, вторая копия не нужна.")
    # при любом выходе курсор должен остаться свободным
    import atexit
    atexit.register(lambda: user32.ClipCursor(None))
    return h


def trigger_spec(cfg):
    """Что считать нажатием кнопки записи: из мастера или стандартная кнопка G10S Pro."""
    t = cfg.get("trigger")
    if t and t.get("type") == "hid":
        return t
    return {"type": "hid", "vid": cfg["vid"], "pid": cfg["pid"], "usage_page": 0x0C, "report": [3, 0xCF, 0]}


def open_remote(cfg):
    t = trigger_spec(cfg)
    path = next((d["path"] for d in hid.enumerate(t["vid"], t["pid"])
                 if d["usage_page"] == t["usage_page"]), None)
    if not path:
        return None
    dev = hid.device()
    dev.open_path(path)
    return dev


STATES = {   # имя: (цвет, подсказка)
    "idle": ("#8e8e93", "ждёт кнопку"),
    "recording": ("#ff3b30", "идёт запись"),
    "paused": ("#ff9f0a", "пауза: нажмите кнопку, чтобы продолжить"),
    "sent": ("#34c759", "сообщение отправлено"),
    "off": ("#3a3a3c", "выключено"),
}
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "MyFlyMouse Voice.lnk")


def make_icon(color):
    """Круг цвета состояния с белым микрофоном."""
    from PIL import Image, ImageDraw
    S = 128   # рисуем крупно, система сама уменьшит до размера значка
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2, 2, S - 3, S - 3), radius=26, fill="#2b2b29", outline="#55554f", width=3)
    try:
        r = Image.open(os.path.join(ASSETS, "remote.png")).convert("RGBA")
        h = S - 22
        r = r.resize((max(1, int(h * r.width / r.height)), h), Image.LANCZOS)
        im.alpha_composite(r, ((S - r.width) // 2 - 6, 11))
    except Exception:
        d.rounded_rectangle((48, 14, 80, 114), radius=14, fill="#555")   # нет картинки пульта: простой силуэт
    # цвет состояния: кружок в правом нижнем углу (серый ждёт, красный запись, оранжевый пауза, зелёный отправлено)
    d.ellipse((S - 56, S - 56, S - 6, S - 6), fill=color, outline="#2b2b29", width=5)
    return im


def autostart_enabled():
    return os.path.exists(STARTUP_LNK)


def set_autostart(on):
    if not on:
        if os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
        return
    import subprocess
    if FROZEN:      # у собранной версии сторож встроен в сам .exe
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.WindowStyle=7;$s.Save()" % (STARTUP_LNK, sys.executable, HERE))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)
        return
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    sup = os.path.join(HERE, "supervisor.ps1")
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='powershell.exe';$s.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%s\"';"
          "$s.WorkingDirectory='%s';$s.WindowStyle=7;$s.Save()" % (STARTUP_LNK, sup, HERE))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)


class App:
    """Значок в трее + меню. Слушатель пульта работает в фоновом потоке."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = True
        self.rec = Recorder(cfg)
        self.rec.on_state = self.set_state
        self.rec.notify = self._notify
        import atexit
        atexit.register(self._release_all)
        self.state = "idle"
        self.icon = None
        self._flash = None
        self._last_press = 0.0
        t = cfg.get("trigger") or {}
        if t.get("type") == "key":   # мастер научил кнопку-клавишу: целиком подменяем её на «записать»
            # только если клавиша пришла с пульта (источник узнаём из Raw Input): такая же клавиша на обычной
            # клавиатуре работает как обычно, иначе, например, стрелка влево перестала бы двигать курсор везде
            self.rec.clicks.key_rules[t["vk"]] = (lambda: True, lambda: self._press(), lambda: True)

    # --- состояние значка ---
    def set_state(self, name):
        if not self.enabled and name != "off":
            return
        self.state = name
        if self._flash:
            self._flash.cancel()
        if self.icon:
            color, tip = STATES[name]
            self.icon.icon = make_icon(color)
            self.icon.title = "AirDictate: " + tip
        if name == "sent":  # зелёный на пару секунд, потом обратно в «ждёт»
            self._flash = threading.Timer(2.0, lambda: self.set_state("idle"))
            self._flash.daemon = True
            self._flash.start()

    # --- слушатель пульта ---
    def listen(self):
        cfg, rec = self.cfg, self.rec
        dev = None
        last_press = 0.0
        log("Запущен. Жду кнопку микрофона на пульте.")
        while True:
            try:
                if dev is None:
                    dev = open_remote(cfg)
                    if dev is None:
                        time.sleep(3)  # приёмник не вставлен - ждём
                        continue
                    log("Пульт подключён")
                data = dev.read(8, 1000)  # таймаут 1 с, чтобы замечать отключение
            except Exception as e:
                log("пульт отключён:", repr(e))
                dev = None
                time.sleep(2)
                continue
            if (cfg.get("trigger") or {}).get("type") == "key":
                continue    # мастер выбрал клавишу: родная кнопка микрофона больше не запускает запись
            rep = trigger_spec(cfg)["report"]
            if not (data and list(data[:len(rep)]) == rep):
                continue
            self._press()
            try:
                # пока искали кнопку, вы могли нажать ещё раз: эти нажатия устарели, сбрасываем их.
                # ВАЖНО: read(n, 0) в этой библиотеке ЖДЁТ БЕСКОНЕЧНО, поэтому читаем в неблокирующем режиме.
                dev.set_nonblocking(1)
                for _ in range(64):
                    if not dev.read(8):
                        break
                dev.set_nonblocking(0)
            except Exception:
                try:
                    dev.set_nonblocking(0)
                except Exception:
                    pass

    def _press(self):
        """Нажата кнопка записи (с пульта или клавишей): старт или остановка с отправкой."""
        if not self.enabled:
            return
        now = time.time()
        # дребезг и повторные нажатия, пока идёт распознавание и отправка
        if now - self._last_press < 1.0:
            log("нажатие проигнорировано")
            return
        self._last_press = now
        try:
            self.rec.stop() if self.rec.active else self.rec.start()
        except Exception as e:
            log("ОШИБКА:", repr(e))
            self.rec.stop(send=False)

    # --- меню ---
    def _toggle(self, key):
        def act(icon, item):
            self.cfg[key] = not self.cfg.get(key, True)
            save_cfg(self.cfg)
        return act

    def _set_silence(self, secs):
        def act(icon, item):
            self.cfg["silence_seconds"] = secs
            save_cfg(self.cfg)
        return act

    def _notify(self, text):
        """Всплывающее уведомление у часов (не чаще раза в 20 секунд)."""
        now = time.time()
        if now - getattr(self, "_last_notify", 0) < 20 or not self.icon:
            return
        self._last_notify = now
        try:
            self.icon.notify(text, "AirDictate")
        except Exception:
            pass

    def _release_all(self):
        """Вернуть всё как было: курсор свободен, кнопки мыши отпущены, панель набирателя закрыта."""
        try:
            user32.ClipCursor(None)
            if self.rec.right_is_down:
                self.rec._right_up()
            if self.rec.active:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.rec.numpad.hide()
        except Exception:
            pass

    def _toggle_enabled(self, icon, item):
        self.enabled = not self.enabled
        self.rec.clicks.enabled = self.enabled   # выкл.: Меню, стрелки и набиратель снова обычные клавиши
        if not self.enabled:
            self._release_all()
        if not self.enabled and self.rec.active:
            self.rec.stop(send=False)
        self.set_state("idle" if self.enabled else "off")
        log("включено" if self.enabled else "выключено из меню")

    def _toggle_autostart(self, icon, item):
        set_autostart(not autostart_enabled())

    def _wizard(self, icon, item):
        """Мастер сам остановит программу на время настройки и запустит её заново."""
        subprocess.Popen(self_cmd("--wizard", "--stop-app"), creationflags=0x00000008)

    def _open(self, path):
        def act(icon, item):
            os.startfile(path)
        return act

    def _quit(self, icon, item):
        try:
            with open(os.path.join(HERE, "stopped.flag"), "w") as f:   # сторож не должен её перезапускать
                f.write("exit")
        except Exception:
            pass
        try:
            if self.rec.active:
                self.rec.stop(send=False)
            self._release_all()
        finally:
            icon.stop()
            os._exit(0)

    def menu(self):
        import pystray
        from pystray import MenuItem as I
        pauses = [I("%s с" % str(s).replace(".", ","), self._set_silence(s),
                    checked=lambda item, s=s: self.cfg["silence_seconds"] == s, radio=True)
                  for s in (1.5, 2.2, 3.0, 4.0)]
        return pystray.Menu(
            I(lambda item: "Статус: " + STATES[self.state][1], None, enabled=False),
            pystray.Menu.SEPARATOR,
            I("Реагировать на пульт", self._toggle_enabled, checked=lambda item: self.enabled),
            I("Пауза до остановки записи", pystray.Menu(*pauses)),
            I("Отправлять сообщение автоматически", self._toggle("press_enter"),
              checked=lambda item: self.cfg.get("press_enter", True)),
            I("Подсвечивать курсор", self._toggle("highlight"),
              checked=lambda item: self.cfg.get("highlight", True)),
            I("Клавиатура при наведении на строку ввода", self._toggle("hover_keyboard"),
              checked=lambda item: self.cfg.get("hover_keyboard", True)),
            I("Системный набиратель (в любых полях ввода)", self._toggle("system_keyboard"),
              checked=lambda item: self.cfg.get("system_keyboard", True)),
            I("Запускать вместе с Windows", self._toggle_autostart, checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            I("Мастер настройки пульта…", self._wizard),
            I("Открыть журнал", self._open(LOG_PATH)),
            I("Открыть настройки", self._open(CFG_PATH)),
            pystray.Menu.SEPARATOR,
            I("Выход", self._quit),
        )

    def run(self):
        import pystray
        threading.Thread(target=self.listen, daemon=True).start()
        self.icon = pystray.Icon("AirDictate", make_icon(STATES["idle"][0]),
                                 "AirDictate: " + STATES["idle"][1], self.menu())
        # меню и по левому клику: на пульте левый клик - центральная кнопка OK, а правый неудобен
        try:
            from pystray._util import win32 as pw
            on_notify = self.icon._on_notify
            self.icon._message_handlers[pw.WM_NOTIFY] = lambda w, l: on_notify(
                w, pw.WM_RBUTTONUP if l == pw.WM_LBUTTONUP else l)
        except Exception as e:
            log("меню по левому клику не включено:", repr(e))

        def setup(icon):
            icon.visible = True
            log("значок в трее создан, visible =", icon.visible)

        try:
            self.icon.run(setup)  # блокирует главный поток
        except Exception as e:
            log("ОШИБКА трея:", repr(e))
            raise


def run(cfg):
    _mutex = single_instance()
    if not cfg.get("setup_done"):
        # первый запуск: сначала мастер (он сам запишет настройки), потом программа
        try:
            subprocess.run(self_cmd("--wizard"), timeout=900)
        except Exception as e:
            log("мастер настройки не отработал:", repr(e))
        cfg = load_cfg()
    # диагностика: причина любого завершения и аварий попадает в журнал
    import atexit, faulthandler
    try:
        faulthandler.enable(open(os.path.join(HERE, "crash.log"), "a"))   # падения на уровне Python/ОС
    except Exception:
        pass
    atexit.register(lambda: log("программа завершает работу"))
    sys.excepthook = lambda t, v, tb: log("ФАТАЛЬНАЯ ОШИБКА:", repr(v))
    threading.excepthook = lambda a: log("ошибка в потоке:", repr(a.exc_value))
    threading.Thread(target=ensure_watchdog, daemon=True).start()   # задача-сторож на месте (создаётся при первом запуске)
    App(cfg).run()


if __name__ == "__main__":
    if "--watchdog" in sys.argv:
        watchdog()
    else:
        run(load_cfg())



