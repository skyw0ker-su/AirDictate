"""Жест «как на телефоне» для кнопки OK пульта.

Зажали OK и ведёте пульт вверх-вниз: страница едет за рукой (колесо мыши), текст не выделяется,
указатель стоит на месте. Отпустили: страница плавно докатывается по инерции. Короткое нажатие
остаётся обычным кликом. Держите OK без движения (по умолчанию 0,4 с): включается обычное зажатие
мыши, то есть выделение текста и перетаскивание, как раньше. Что оно включилось, показывает кольцо
у указателя (HoldMark), звук по желанию: звука может не быть.

Жест не вмешивается там, где его быть не должно: обычная мышь, заголовок и рамка окна (окно тянут
как раньше), полосы прокрутки окон Windows, панель задач и рабочий стол, окна самой программы,
окна с повышенными правами (туда ввод всё равно не пройдёт).

Состояния: IDLE -> PENDING (OK нажата, ждём движения или долгого нажатия) -> SCROLL или DRAG -> IDLE.
Хук мыши только решает «проглотить или пропустить» и кладёт действия в очередь: вызывать ввод мыши
прямо из хука нельзя. Всё, что связано с Windows, собрано в WinEnv, а сам жест (TouchScroll) от
Windows не зависит и проверяется тестами (test_touch_scroll.py).
"""
import collections
import math
import queue
import threading
import time

IDLE, PENDING, SCROLL, DRAG = "idle", "pending", "scroll", "drag"
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WHEEL_DELTA = 120            # одно «деление» колеса
FLING_WINDOW = 0.10          # по движению за последние 0,1 с считаем скорость для инерции
FLING_MIN = 1.5 * WHEEL_DELTA   # медленнее (делений колеса в секунду) не докатываем
FLING_MAX = 60 * WHEEL_DELTA
FLING_STOP = 0.5 * WHEEL_DELTA
STUCK_SECONDS = 60.0         # жест без единого события дольше этого считаем зависшим


class TouchScroll:
    def __init__(self, env, cfg, log=None, clock=time.monotonic, start=True):
        self.env, self.cfg = env, cfg
        self.log = log or (lambda *a: None)
        self.clock = clock
        self.lock = threading.RLock()
        self.state = IDLE
        self.anchor = (0, 0)             # где стоит указатель: движения мы глотаем, он не сдвигается
        self.t0 = self.last_event = 0.0
        self.total = [0.0, 0.0]          # сдвиг с момента нажатия, пока решаем «прокрутка или долгое нажатие»
        self.pending_px = 0.0            # сдвиг по вертикали, ещё не отправленный колесом
        self.pressed = False             # DRAG: настоящее нажатие уже подано в систему
        self.marked = False              # кольцо у указателя сейчас показано
        self.actions = queue.SimpleQueue()
        self.acc = 0.0                   # дробная часть колеса, не отправленная из-за округления
        self.samples = collections.deque(maxlen=64)   # (время, деления колеса): для скорости инерции
        self.fling_v = 0.0               # скорость докатывания, делений колеса в секунду
        self.last_step = 0.0
        self.wake = threading.Event()
        self.alive = True
        if start:
            threading.Thread(target=self._run, daemon=True).start()

    def _c(self, key, default):
        v = self.cfg.get(key, default)
        return default if v is None else v

    # --- поток хука мыши: только решения, ничего долгого ---

    def hook(self, msg, x, y):
        """Вызывается из хука мыши для событий НЕ от нашей эмуляции. True = событие проглотить."""
        try:
            if msg == WM_LBUTTONDOWN:
                return self._down(x, y)
            st = self.state
            if st == IDLE:
                return False
            self.last_event = self.clock()
            if msg == WM_MOUSEMOVE:
                if st == DRAG:
                    return not self.pressed   # пока нажатие не подано, движения не пускаем
                return self._move(x, y)
            if msg == WM_LBUTTONUP:
                return self._up()
        except Exception as e:
            self._fail(e)
        return False

    def _down(self, x, y):
        with self.lock:
            self.fling_v = 0.0   # прикосновение останавливает докатывание
            if self.state != IDLE:
                self._reset("новое нажатие, а прошлый жест не закончен")
        if not self._c("touch_scroll", True):
            return False
        if not (self.env.from_remote() and self.env.can_take(x, y)):
            return False
        now = self.clock()
        with self.lock:
            self.state = PENDING
            self.anchor = (x, y)
            self.t0 = self.last_event = now
            self.total = [0.0, 0.0]
            self.pending_px = 0.0
            self.acc = 0.0
            self.samples.clear()
            self.pressed = False
        self.wake.set()
        return True

    def _move(self, x, y):
        dx, dy = x - self.anchor[0], y - self.anchor[1]
        if not (dx or dy):
            return True
        with self.lock:
            if self.state == PENDING:
                self.total[0] += dx
                self.total[1] += dy
                if math.hypot(self.total[0], self.total[1]) >= self._c("touch_threshold_px", 30):
                    self.state = SCROLL
                    self.pending_px += self.total[1]   # ничего не теряем: прокрутка начинается со всего пройденного
            elif self.state == SCROLL:
                self.pending_px += dy
            else:
                return not self.pressed   # состояние сменил поток таймера (долгое нажатие)
        self.wake.set()
        return True

    def _up(self):
        with self.lock:
            st = self.state
            if st == IDLE:
                return False
            self.state = IDLE
            self.actions.put(({PENDING: "tap", SCROLL: "fling", DRAG: "release"}[st], self.clock()))
        self.wake.set()
        return True

    def _reset(self, why=""):
        with self.lock:
            was = self.state
            self.state = IDLE
            self.pending_px = 0.0
            self.fling_v = 0.0
        if was != IDLE:
            if why:
                self.log("жест OK сброшен: " + why)
            if was == DRAG:
                self.actions.put(("release", self.clock()))

    def _fail(self, e):
        self.log("ошибка жеста OK:", repr(e))
        try:
            self._reset()
        except Exception:
            pass

    def abort(self):
        """Выключили из меню или выходим: бросаем жест, а если держим нажатую кнопку (долгое нажатие), отпускаем её."""
        with self.lock:
            self.state = IDLE
            self.fling_v = 0.0
            self.pending_px = 0.0
        if self.pressed:
            self.pressed = False
            try:
                self.env.button(False)
            except Exception:
                pass
        self._unmark()

    def _unmark(self):
        if self.marked:
            self.marked = False
            try:
                self.env.mark(False)
            except Exception:
                pass

    def close(self):
        self.alive = False
        self.abort()
        self.wake.set()

    # --- рабочий поток: ввод в систему ---

    def _run(self):
        while self.alive:
            self.wake.clear()
            delay = self.process()
            self.wake.wait(delay)

    def process(self):
        """Один шаг рабочего потока. Возвращает, через сколько секунд позвать снова."""
        try:
            while True:
                try:
                    name, t = self.actions.get_nowait()
                except queue.Empty:
                    break
                self._do(name, t)
            now = self.clock()
            self._tick(now)
            self._emit(now)
            self._inertia(now)
            if self.state != IDLE and now - self.last_event > STUCK_SECONDS:
                self._reset("жест завис")
        except Exception as e:
            self._fail(e)
        return 0.008 if (self.state in (PENDING, SCROLL) or self.fling_v) else 0.25

    def _do(self, name, t):
        if name == "tap":
            self.env.notify_click()
            self.env.click()
        elif name == "release":
            self.env.button(False)
            self.pressed = False
            self._unmark()
        elif name == "fling":
            self._start_fling(t)

    def _tick(self, now):
        """Долгое нажатие: держим OK без движения, и дальше всё как у обычной мыши."""
        with self.lock:
            if self.state != PENDING or now - self.t0 < self._c("touch_long_press", 0.4):
                return
            self.state = DRAG
            self.pressed = False
        self.env.notify_click()
        if self._c("touch_mark", True):
            self.marked = True
            self.env.mark(True)
        if self._c("touch_beep", False):
            self.env.beep()
        self.env.button(True)
        self.pressed = True

    def _emit(self, now):
        with self.lock:
            px, self.pending_px = self.pending_px, 0.0
        if not px:
            return
        sign = -1 if self._c("touch_scroll_invert", False) else 1
        self._send(sign * px / self.env.px_per_notch() * WHEEL_DELTA * self._c("touch_scroll_speed", 1.0), now)

    def _send(self, delta, now):
        """Колесо принимает целые числа: дробную часть копим до следующего раза."""
        self.acc += delta
        n = int(self.acc)
        if n:
            self.acc -= n
            self.env.wheel(n)
            self.samples.append((now, n))

    def _start_fling(self, t):
        if not self._c("touch_scroll_inertia", True):
            return
        total = sum(n for ts, n in self.samples if t - ts <= FLING_WINDOW)
        v = max(-FLING_MAX, min(FLING_MAX, total / FLING_WINDOW))
        if abs(v) >= FLING_MIN:
            self.fling_v = v
            self.last_step = t

    def _inertia(self, now):
        v = self.fling_v
        if not v:
            return
        dt = max(0.0, min(now - self.last_step, 0.05))
        self.last_step = now
        self._send(v * dt, now)
        v *= math.exp(-dt / self._c("touch_fling_tau", 0.35))
        self.fling_v = 0.0 if abs(v) < FLING_STOP else v


class HoldMark:
    """Кольцо у указателя, пока включено долгое нажатие (выделение, перетаскивание): видно и без звука.

    Кольцо за 0,2 с «схлопывается» к указателю (понятно, что нажатие сработало), потом идёт за ним.
    Окно прозрачно для мыши и не берёт фокус, как красное кольцо записи (Highlight в voice_remote.py).
    Размеры считаются от масштаба экрана: на 4K при 300% кольцо вокруг указателя иначе не разглядеть.
    """
    COLOR = "#0a84ff"

    def __init__(self, u, scale=1.0, log=None):
        self.u, self.s = u, max(1.0, scale)
        self.log = log or (lambda *a: None)
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def show(self):
        self.q.put(True)

    def hide(self):
        self.q.put(False)

    def _run(self):
        import ctypes
        from ctypes import wintypes
        u, s = self.u, self.s
        S = int(96 * s)                       # сторона окна
        c = S // 2
        r_end, r_start = 14 * s, 36 * s       # радиус кольца у указателя и в начале «схлопывания»
        width = max(2, int(3 * s))
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
            cv = tk.Canvas(win, width=S, height=S, bg=key, highlightthickness=0)
            cv.pack()
            win.update_idletasks()
            top = u.GetParent(win.winfo_id()) or win.winfo_id()
            # LAYERED | TRANSPARENT (клики насквозь) | NOACTIVATE | TOOLWINDOW
            u.SetWindowLongW(top, -20, u.GetWindowLongW(top, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80)
            win.withdraw()
        except Exception as e:
            self.log("кольцо долгого нажатия недоступно:", repr(e))
            return
        pt = wintypes.POINT()
        st = {"on": False, "t0": 0.0, "pos": None}

        def frame():
            try:
                while True:
                    try:
                        on = self.q.get_nowait()
                    except queue.Empty:
                        break
                    st["on"], st["t0"], st["pos"] = on, time.time(), None
                    if on:
                        win.deiconify()
                        win.attributes("-topmost", True)
                    else:
                        win.withdraw()
                if st["on"]:
                    u.GetCursorPos(ctypes.byref(pt))
                    if st["pos"] != (pt.x, pt.y):
                        st["pos"] = (pt.x, pt.y)
                        win.geometry("%dx%d+%d+%d" % (S, S, pt.x - c, pt.y - c))
                    r = r_end + max(0.0, 0.2 - (time.time() - st["t0"])) / 0.2 * (r_start - r_end)
                    cv.delete("all")
                    cv.create_oval(c - r, c - r, c + r, c + r, outline=self.COLOR, width=width)
            except Exception as e:
                self.log("кольцо долгого нажатия:", repr(e))
            root.after(16 if st["on"] else 60, frame)

        frame()
        root.mainloop()


class WinEnv:
    """Всё, что касается Windows: чьё это движение, что под указателем, ввод мыши."""
    GA_ROOT, WM_NCHITTEST, SMTO_ABORTIFHUNG, HTCLIENT = 2, 0x0084, 0x0002, 1
    # на панели задач и рабочем столе жест не нужен: там тянут значки и рамку выделения
    SKIP_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW")
    WHEEL, LEFTDOWN, LEFTUP = 0x0800, 0x0002, 0x0004

    def __init__(self, watcher, cfg, log=None):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.watcher, self.cfg = watcher, cfg
        self.log = log or (lambda *a: None)
        self.pid = ctypes.windll.kernel32.GetCurrentProcessId()
        # свои экземпляры функций: типы аргументов не должны менять то, что настроено в остальной программе
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        u.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._scale = None
        # кольцо долгого нажатия живёт в своём потоке с окном Tk
        self.mark_win = HoldMark(u, self.scale(), self.log) if cfg.get("touch_mark", True) else None

    def from_remote(self):
        return self.watcher.mouse_from_remote()

    def can_take(self, x, y):
        """Жест применяем, только если под указателем содержимое окна, а не его заголовок, рамка и т. п."""
        c, w, u = self.ctypes, self.wintypes, self.u
        hwnd = u.WindowFromPoint(w.POINT(x, y))
        if not hwnd:
            return False
        root = u.GetAncestor(hwnd, self.GA_ROOT) or hwnd
        pid = w.DWORD(0)
        u.GetWindowThreadProcessId(root, c.byref(pid))
        if pid.value == self.pid:
            return False   # окна самой программы (набиратель и т. п.) работают как обычно
        name = c.create_unicode_buffer(64)
        u.GetClassNameW(root, name, 64)
        if name.value in self.SKIP_CLASSES:
            return False
        res = c.c_size_t(0)
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        # нет ответа (окно зависло или у него повышенные права): жест не включаем, мышь работает как обычно
        if not u.SendMessageTimeoutW(root, self.WM_NCHITTEST, 0, lparam, self.SMTO_ABORTIFHUNG, 40, c.byref(res)):
            return False
        return c.c_int32(res.value & 0xFFFFFFFF).value == self.HTCLIENT

    def px_per_notch(self):
        """Сколько пикселей пути указателя соответствует одному делению колеса (в браузерах это 100 «логических» пикселей)."""
        v = self.cfg.get("touch_scroll_px_per_notch") or 0
        return float(v) if v else 100.0 * self.scale()

    def scale(self):
        """Масштаб экрана (3,0 = 300%): пиксели физические, а размеры интерфейса считаем от логических."""
        if self._scale is None:
            try:
                self._scale = self.u.GetDpiForSystem() / 96.0
            except Exception:
                self._scale = 1.0
        return self._scale

    def wheel(self, delta):
        self.u.mouse_event(self.WHEEL, 0, 0, delta & 0xFFFFFFFF, 0)

    def button(self, down):
        self.u.mouse_event(self.LEFTDOWN if down else self.LEFTUP, 0, 0, 0, 0)

    def click(self):
        self.button(True)
        time.sleep(0.015)
        self.button(False)

    def notify_click(self):
        self.watcher.event.set()   # как и раньше, настоящий клик OK ставит запись на паузу

    def beep(self):
        try:
            import winsound
            winsound.Beep(1100, 25)
        except Exception:
            pass

    def mark(self, on):
        if self.mark_win:
            (self.mark_win.show if on else self.mark_win.hide)()


def create(watcher, cfg, log=None):
    return TouchScroll(WinEnv(watcher, cfg, log), cfg, log)
