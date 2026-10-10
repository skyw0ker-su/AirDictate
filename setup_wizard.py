"""Мастер настройки AirDictate.

Учит программу вашему пульту: какая кнопка запускает запись, какой микрофон у пульта.
Запуск: setup.bat, пункт «Мастер настройки» в меню значка, либо автоматически при первом запуске.
"""
import collections, ctypes, math, os, queue, re, subprocess, sys, threading, time

import tkinter as tk
import tkinter.font as tkfont
import hid
import numpy as np
import sounddevice as sd

import voice_remote as v   # поднимает DPI-режим и общие функции (настройки, автозапуск)

HERE = v.HERE
STOP_APP = "--stop-app" in sys.argv     # остановить работающую программу на время настройки
S = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)

# палитра тёмной темы приложения Claude
INK, PANEL, CREAM, MIST = "#262624", "#30302e", "#faf9f5", "#a8a69c"
LINE = "#43433f"
BRASS, BRASS_HI, OK, WARN = "#d97757", "#e48a6b", "#8bc08a", "#e0a33d"


def F(size, bold=False, serif=False):
    if serif:
        return ("Georgia", -int(size * S))
    return ("Segoe UI Semibold" if bold else "Segoe UI", -int(size * S))


class RoundButton(tk.Canvas):
    """Кнопка со скруглёнными углами; настраивается как обычная (state, bg, fg)."""

    def __init__(self, parent, text, command, bg, fg, hover, font, padx, pady):
        self._f = tkfont.Font(font=font)
        w, h = self._f.measure(text) + 2 * padx, self._f.metrics("linespace") + 2 * pady
        super().__init__(parent, width=w, height=h, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        self.text, self.command, self.bg, self.fg, self.hover, self.font = text, command, bg, fg, hover, font
        self.w, self.h, self.enabled, self.over = w, h, True, False
        self.bind("<Enter>", lambda e: self._set_over(True))
        self.bind("<Leave>", lambda e: self._set_over(False))
        self.bind("<ButtonRelease-1>", lambda e: self.enabled and self.command and self.command())
        self.draw()

    def _set_over(self, v):
        self.over = v
        self.draw()

    def draw(self):
        self.delete("all")
        r = self.h // 2
        col = self.hover if (self.over and self.enabled) else self.bg
        pts = [r, 0, self.w - r, 0, self.w, 0, self.w, r, self.w, self.h - r, self.w, self.h,
               self.w - r, self.h, r, self.h, 0, self.h, 0, self.h - r, 0, r, 0, 0]
        self.create_polygon(pts, smooth=True, fill=col, outline=col)
        self.create_text(self.w // 2, self.h // 2, text=self.text, fill=self.fg, font=self.font)
        self.configure(cursor="hand2" if self.enabled else "arrow")

    def configure(self, cnf=None, **kw):
        if any(k in kw for k in ("state", "bg", "fg")):
            if "state" in kw:
                self.enabled = kw.pop("state") != "disabled"
            self.bg = kw.pop("bg", self.bg)
            self.fg = kw.pop("fg", self.fg)
            self.draw()
        if kw or cnf:
            super().configure(cnf, **kw)
    config = configure


def stop_app():
    """Остановить работающую программу и сторожа, освободить курсор."""
    try:
        with open(os.path.join(HERE, "stopped.flag"), "w") as f:
            f.write("setup")
    except Exception:
        pass
    if v.FROZEN:      # собранная версия: гасим все AirDictate.exe, кроме самого мастера
        subprocess.run(["taskkill", "/F", "/FI", "IMAGENAME eq AirDictate.exe", "/FI", "PID ne %d" % os.getpid()],
                       creationflags=0x08000000)
    else:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*supervisor.ps1*' "
              "-or $_.CommandLine -like '*voice_remote.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000)
    ctypes.windll.user32.ClipCursor(None)


def hid_label(info):
    name = (info.get("product_string") or "").strip()
    return "%s (VID_%04X&PID_%04X)" % (name or "устройство", info["vendor_id"], info["product_id"])


class HidListener:
    """Читает входные отчёты со всех доступных HID-устройств, кроме мыши и клавиатуры (их читает система)."""

    def __init__(self, only=None):
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.threads = []
        seen = set()
        for info in hid.enumerate():
            if (info["usage_page"], info["usage"]) in ((1, 2), (1, 6)):
                continue
            if only and (info["vendor_id"], info["product_id"], info["usage_page"]) != only:
                continue
            if info["path"] in seen:
                continue
            seen.add(info["path"])
            t = threading.Thread(target=self._read, args=(info,), daemon=True)
            t.start()
            self.threads.append(t)

    def _read(self, info):
        try:
            d = hid.device()
            d.open_path(info["path"])
        except Exception:
            return
        try:
            while not self.stop.is_set():
                data = d.read(16, 200)
                if data:
                    self.q.put((info, bytes(data)))
        except Exception:
            pass
        finally:
            try:
                d.close()
            except Exception:
                pass

    def close(self):
        self.stop.set()


CAPTIONS = {
    0: ("Вот эта кнопка с микрофоном", "нажимать будем именно её", False),
    1: ("Нажмите её 3 раза", "с паузой около секунды", False),
    2: ("Скажите пару слов", "прямо в микрофон пульта", True),
    3: ("Нажмите ещё раз", "для проверки", False),
    4: ("Готово!", "пульт доволен", False),
}


class Stage(tk.Canvas):
    """Танцующий пульт: подсвечивает кнопку записи пульсирующими кольцами и подписывает, что делать на этом шаге."""
    MIC = (0.481, 0.488)      # где кнопка микрофона на картинке пульта (доли ширины и высоты)

    def __init__(self, parent, step):
        super().__init__(parent, bg=INK, highlightthickness=0)
        from PIL import Image
        self.Image = Image
        self.src = Image.open(os.path.join(v.ASSETS, "remote.png")).convert("RGBA")
        self.step = step
        self.t0 = time.time()
        self.photo = None
        self.job = None
        self.bind("<Destroy>", lambda e: self.job and self.after_cancel(self.job))
        self.tick()

    def tick(self):
        try:
            self.draw()
        except Exception:
            return
        self.job = self.after(40, self.tick)

    def draw(self):
        from PIL import ImageTk
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 80:
            return
        t = time.time() - self.t0
        done = self.step == 4
        beat = t * 104 / 60                      # такт 104 удара в минуту, как в видео
        ph = beat % 1
        hop = abs(math.sin(math.pi * ph)) * (1 if (done or self.step in (0, 1, 3)) else 0.35)
        sway = math.sin(math.pi * beat / 2)
        bh = H * 0.62
        bw = bh * 214 / 703
        leg = H * 0.12
        arm = H * 0.13
        cx = W * 0.30
        gy = H * 0.93
        up = hop * H * 0.045
        tilt = 0.10 * math.sin(math.pi * beat)
        self.delete("all")
        # пол и тень
        self.create_oval(cx - bw * 1.5, gy - 4 * S, cx + bw * 1.5, gy + 8 * S, fill="#1d1d1b", outline="")
        ox = cx + sway * H * 0.04
        # ноги
        swing = math.sin(math.pi * beat)
        for hx, ang in ((-bw * 0.22, -0.5 * max(0, swing)), (bw * 0.22, 0.5 * max(0, -swing))):
            x0, y0 = ox + hx, gy - up - leg
            x1, y1 = x0 + math.sin(ang) * leg, y0 + math.cos(ang) * leg
            self.create_line(x0, y0, x1, y1, fill=CREAM, width=int(5 * S), capstyle="round")
            self.create_oval(x1 - 12 * S + (-4 if hx < 0 else 4) * S, y1 - 4 * S, x1 + 12 * S + (-4 if hx < 0 else 4) * S, y1 + 7 * S, fill=BRASS, outline="")
        # руки
        sh_y = gy - up - leg - bh * 0.58
        for sx, ph0 in ((-bw * 0.5, 0.3), (bw * 0.5, math.pi + 0.3)):
            ang = 1.15 + 0.95 * math.sin(math.pi * beat + ph0)
            ang = -ang if sx < 0 else ang
            x1, y1 = ox + sx + math.sin(ang) * arm, sh_y + math.cos(ang) * arm
            self.create_line(ox + sx, sh_y, x1, y1, fill=CREAM, width=int(4 * S), capstyle="round")
            self.create_oval(x1 - 6 * S, y1 - 6 * S, x1 + 6 * S, y1 + 6 * S, fill="#ffffff", outline="")
        # корпус (с наклоном)
        im = self.src.resize((max(1, int(bw)), max(1, int(bh))), self.Image.LANCZOS)
        rot = im.rotate(-math.degrees(tilt), expand=True, resample=self.Image.BICUBIC)
        self.photo = ImageTk.PhotoImage(rot)
        by = gy - up - leg - bh / 2
        self.create_image(ox, by, image=self.photo)
        # кольца вокруг кнопки микрофона
        mx = ox + (self.MIC[0] - 0.5) * bw
        my = by + (self.MIC[1] - 0.5) * bh
        for k in range(3):
            p = ((t * 0.9) + k / 3) % 1
            r = (6 + p * 34) * S
            col = BRASS if self.step != 2 else OK
            fade = ("#d97757", "#a8583f", "#6e3f30", "#43433f")[min(3, int(p * 4))] if self.step != 2 else ("#8bc08a", "#6a9a69", "#4a6e4a", "#43433f")[min(3, int(p * 4))]
            self.create_oval(mx - r, my - r, mx + r, my + r, outline=fade, width=int(2.5 * S))
        pulse = 0.5 + 0.5 * math.sin(t * 6)
        rr = (5 + 2 * pulse) * S
        self.create_oval(mx - rr, my - rr, mx + rr, my + rr, fill=BRASS if self.step != 2 else OK, outline="")
        if CAPTIONS[self.step][2]:                    # звуковые дуги у микрофона
            for k in range(3):
                a = ((t * 1.4) + k / 3) % 1
                r2 = (14 + a * 30) * S
                self.create_arc(mx - bw * 0.9 - r2, my - r2, mx - bw * 0.9 + r2, my + r2, start=-40, extent=80, style="arc",
                                outline=("#8bc08a", "#6a9a69", "#4a6e4a", "#43433f")[min(3, int(a * 4))], width=int(2.5 * S))
        # линия-указатель к подписи и сама подпись
        head, sub, _ = CAPTIONS[self.step]
        tx = W * 0.52
        self.create_line(mx + 26 * S, my, tx - 14 * S, my, fill=BRASS if self.step != 2 else OK, width=int(2 * S), dash=(4, 4))
        self.create_text(tx, my - 14 * S, text=head, anchor="w", fill=CREAM, font=F(22, serif=True))
        self.create_text(tx, my + 16 * S, text=sub, anchor="w", fill=MIST, font=F(15))
        # на шаге 1: три точки, по очереди загораются в такт ожиданию нажатий
        if self.step == 1:
            lit = int(t * 0.7) % 4
            for i in range(3):
                x = tx + (i * 26 + 8) * S
                on = i < lit
                self.create_oval(x - 8 * S, my + 44 * S, x + 8 * S, my + 60 * S, fill=BRASS if on else INK, outline=BRASS if on else LINE, width=int(1.5 * S))


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AirDictate: настройка пульта")
        self.configure(bg=INK)
        w, h = int(780 * S), int(640 * S)
        self.geometry("%dx%d+%d+%d" % (w, h, (self.winfo_screenwidth() - w) // 2, (self.winfo_screenheight() - h) // 3))
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.cand = None            # найденная кнопка записи
        self.use_default = False    # шаг 1 пропущен: оставляем стандартную кнопку микрофона G10S Pro
        self.audio_name = None      # выбранный микрофон
        self.autostart = tk.BooleanVar(value=True)
        self.host = tk.Frame(self, bg=INK)
        self.host.pack(fill="both", expand=True, padx=int(28 * S), pady=int(22 * S))
        self.step = 0
        self.after_job = None
        self.cleanup = lambda: None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.show(0)

    # ---------- общие куски ----------
    def clear(self):
        self.cleanup()
        self.cleanup = lambda: None
        if self.after_job:
            self.after_cancel(self.after_job)
            self.after_job = None
        for w in self.host.winfo_children():
            w.destroy()

    def title_label(self, text, sub=None, n=None):
        top = tk.Frame(self.host, bg=INK)
        top.pack(fill="x", pady=(0, int(16 * S)))
        tk.Label(top, text="AirDictate", font=F(15, True), fg=BRASS, bg=INK).pack(side="left")
        if n:
            dots = tk.Canvas(top, width=int(4 * 22 * S), height=int(12 * S), bg=INK, highlightthickness=0)
            dots.pack(side="right")
            for i in range(4):
                x = int((i * 22 + 8) * S)
                on = i < n
                dots.create_oval(x - int(5 * S), int(1 * S), x + int(5 * S), int(11 * S),
                                 fill=BRASS if on else INK, outline=BRASS if on else LINE, width=int(1.5 * S))
            tk.Label(top, text="Шаг %d из 4" % n, font=F(14), fg=MIST, bg=INK).pack(side="right", padx=int(10 * S))
        tk.Frame(self.host, bg=LINE, height=1).pack(fill="x", pady=(0, int(18 * S)))
        tk.Label(self.host, text=text, font=F(32, serif=True), fg=CREAM, bg=INK, wraplength=int(700 * S), justify="left").pack(anchor="w", pady=(0, int(8 * S)))
        if sub:
            tk.Label(self.host, text=sub, font=F(17), fg=MIST, bg=INK, wraplength=int(700 * S), justify="left").pack(anchor="w", pady=(0, int(14 * S)))

    def card(self, **kw):
        """Карточка с тонкой рамкой, как в интерфейсе Claude."""
        return tk.Frame(self.host, bg=PANEL, highlightbackground=LINE, highlightthickness=1, **kw)

    def button(self, parent, text, cmd, primary=True):
        return RoundButton(parent, text, cmd, bg=BRASS if primary else PANEL, fg="#1f1e1d" if primary else CREAM,
                           hover=BRASS_HI if primary else "#3a3a38", font=F(16, True),
                           padx=int(26 * S), pady=int(9 * S))

    def nav(self, back=None, nxt=None, nxt_text="Дальше", nxt_enabled=True, skip=None, skip_text="Пропустить"):
        bar = tk.Frame(self.host, bg=INK)
        bar.pack(side="bottom", fill="x", pady=(int(10 * S), 0))
        if back is not None:
            self.button(bar, "Назад", back, primary=False).pack(side="left")
        if skip is not None:
            self.button(bar, skip_text, skip, primary=False).pack(side="left", padx=int(10 * S))
        Stage(self.host, self.step).pack(side="top", fill="both", expand=True, pady=(int(6 * S), 0))
        nb = self.button(bar, nxt_text, nxt) if nxt else None
        if nb:
            nb.pack(side="right")
            if not nxt_enabled:
                nb.configure(state="disabled", bg=PANEL, fg=MIST)
        return nb

    def enable(self, btn):
        btn.configure(state="normal", bg=BRASS, fg="#1f1e1d")

    def show(self, i):
        self.clear()
        self.step = i
        [self.s_welcome, self.s_button, self.s_mic, self.s_test, self.s_finish][i]()

    def cancel(self):
        self.cleanup()
        self.destroy()

    # ---------- шаг 0: приветствие ----------
    def s_welcome(self):
        self.title_label("Настроим ваш пульт",
                         "Программа должна узнать, какая кнопка на пульте начинает запись и какой микрофон у пульта. "
                         "Это займёт около минуты. Пульт должен быть включён, а его USB-приёмник вставлен в компьютер.")
        card = self.card()
        card.pack(fill="x", pady=int(10 * S))
        tk.Label(card, text="Что понадобится", font=F(14, True), fg=MIST, bg=PANEL).pack(anchor="w", padx=int(20 * S), pady=(int(14 * S), int(4 * S)))
        for i, line in enumerate(("Нажать кнопку записи на пульте 3 раза", "Сказать пару слов в микрофон пульта", "Нажать кнопку ещё раз для проверки"), 1):
            r = tk.Frame(card, bg=PANEL)
            r.pack(anchor="w", padx=int(20 * S), pady=int(4 * S))
            tk.Label(r, text=str(i), font=F(15, True), fg="#1f1e1d", bg=BRASS, width=2).pack(side="left", padx=(0, int(12 * S)))
            tk.Label(r, text=line, font=F(18), fg=CREAM, bg=PANEL).pack(side="left")
        tk.Frame(card, bg=PANEL, height=int(10 * S)).pack()
        self.nav(nxt=lambda: self.show(1), nxt_text="Начать", skip=self.skip_setup)

    def skip_setup(self):
        cfg = v.load_cfg(); cfg["setup_done"] = True; v.save_cfg(cfg)
        self.cancel()

    # ---------- шаг 1: кнопка записи ----------
    def s_button(self):
        self.title_label("Нажмите кнопку записи",
                         "Нажмите на пульте кнопку, которой вы хотите начинать запись (обычно с значком микрофона). Нажмите её три раза, с паузой около секунды.", 1)
        card = self.card()
        card.pack(fill="x", pady=int(8 * S))
        status = tk.Label(card, text="Жду нажатий…", font=F(20, True), fg=WARN, bg=PANEL, justify="left", wraplength=int(650 * S))
        status.pack(anchor="w", padx=int(20 * S), pady=(int(16 * S), int(4 * S)))
        detail = tk.Label(card, text=" ", font=F(15), fg=MIST, bg=PANEL, justify="left", wraplength=int(650 * S))
        detail.pack(anchor="w", padx=int(20 * S), pady=(0, int(16 * S)))
        tk.Label(self.host, text="Пульт G10S Pro? Тогда кнопка с микрофоном уже настроена, этот шаг можно пропустить. "
                                 "Нужна другая кнопка или другое устройство: нажмите её три раза.",
                 font=F(14), fg=MIST, bg=INK, wraplength=int(700 * S), justify="left").pack(anchor="w", pady=int(10 * S))

        def skip_default():
            self.cand = None
            self.use_default = True
            self.show(2)
        nxt = self.nav(back=lambda: self.show(0), nxt=lambda: self.show(2), nxt_enabled=False,
                       skip=skip_default, skip_text="Пропустить: у меня G10S Pro")

        hidl = HidListener()
        cw = v.ClickWatcher(remote_tag=None)    # клавиатура: хук и Raw Input (видим, с какого устройства клавиша)
        counts = collections.Counter()
        keys = collections.Counter()
        seen_raw = [0]
        info_of = {}
        self.cleanup = hidl.close

        def poll():
            try:
                while True:
                    info, data = hidl.q.get_nowait()
                    if any(data[1:]):                          # байт 0 это номер отчёта; «отпустили» = остальные нули
                        k = (info["path"], data)
                        counts[k] += 1
                        info_of[k] = info
            except queue.Empty:
                pass
            items = list(cw.raw)
            for e in items[seen_raw[0]:]:
                keys[(e[1], e[4])] += 1
            seen_raw[0] = len(items)

            best = None
            for k, n in counts.items():
                if 3 <= n <= 40 and (best is None or n > counts[best]):
                    best = k
            bestkey = None
            for k, n in keys.items():
                if 3 <= n <= 12 and (bestkey is None or n > keys[bestkey]):
                    bestkey = k
            total = sum(counts.values()) + sum(keys.values())
            if best or bestkey:
                self.use_default = False
            if best:
                info = info_of[best]
                self.cand = dict(kind="hid", vid=info["vendor_id"], pid=info["product_id"], usage_page=info["usage_page"],
                                 report=list(best[1]), label=hid_label(info))
                status.configure(text="Кнопка найдена", fg=OK)
                detail.configure(text="Устройство: %s\nСигнал: %s" % (self.cand["label"], " ".join("%02X" % b for b in best[1])))
                self.enable(nxt)
            elif bestkey:
                vk, name = bestkey
                m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", name.upper())
                self.cand = dict(kind="key", vk=vk, vid=int(m.group(1), 16) if m else None, pid=int(m.group(2), 16) if m else None,
                                 label=name or "клавиатура")
                status.configure(text="Кнопка найдена (клавиша)", fg=OK)
                detail.configure(text="Клавиша: код 0x%02X\nУстройство: %s" % (vk, name or "неизвестно"))
                self.enable(nxt)
            else:
                status.configure(text="Жду нажатий…" if not total else "Нажмите ту же кнопку ещё раз (всего нужно 3 раза)", fg=WARN)
            self.after_job = self.after(150, poll)

        poll()

    # ---------- шаг 2: микрофон ----------
    def s_mic(self):
        self.title_label("Выберите микрофон пульта",
                         "Скажите несколько слов прямо в пульт, поднеся его ко рту. Полоски покажут, какие микрофоны вас слышат (веб-камера тоже слышит). Затем отметьте тот, что стоит в пульте.", 2)
        listbox = tk.Frame(self.host, bg=INK)
        listbox.pack(fill="x", pady=int(6 * S))
        nxt = self.nav(back=lambda: self.show(1), nxt=lambda: self.show(3), nxt_enabled=False)

        devices = {}
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0:
                continue
            if sd.query_hostapis(d["hostapi"])["name"] != "MME":
                continue
            name = d["name"]
            if any(x in name for x in ("Переназначение", "Sound Mapper", "Primary")):
                continue
            devices[name] = (i, int(d["default_samplerate"]))
        level = {n: 0.0 for n in devices}
        peak = {n: 0.0 for n in devices}
        streams = []

        def make_cb(n):
            def cb(indata, frames, t, status):
                r = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
                level[n] = r
                peak[n] = max(peak[n], r)
            return cb
        for n, (i, sr) in devices.items():
            try:
                st = sd.InputStream(device=i, channels=1, samplerate=sr, blocksize=sr // 10, callback=make_cb(n))
                st.start()
                streams.append(st)
            except Exception:
                level.pop(n, None); peak.pop(n, None)

        def close():
            for st in streams:
                try:
                    st.stop(); st.close()
                except Exception:
                    pass
        self.cleanup = close

        # Выбор только руками: у многих есть ещё и веб-камера, и она тоже «слышит» речь,
        # поэтому ничего не отмечаем сами, а лишь подсказываем, какой микрофон слышит громче всех.
        chosen = [None]
        rows = {}
        ring = int(11 * S)

        def pick(n):
            chosen[0] = n
            self.audio_name = n
            self.enable(nxt)

        for n in level:
            row = tk.Frame(listbox, bg=PANEL, highlightbackground=LINE, highlightthickness=1, cursor="hand2")
            row.pack(fill="x", pady=int(3 * S))
            mark = tk.Canvas(row, width=2 * ring + 4, height=2 * ring + 4, bg=PANEL, highlightthickness=0)
            mark.pack(side="left", padx=(int(14 * S), int(8 * S)), pady=int(10 * S))
            name = tk.Label(row, text=n, font=F(16), fg=CREAM, bg=PANEL, anchor="w")
            name.pack(side="left")
            badge = tk.Label(row, text="", font=F(13, True), fg=OK, bg=PANEL)
            badge.pack(side="left", padx=int(12 * S))
            bar = tk.Canvas(row, width=int(170 * S), height=int(10 * S), bg=INK, highlightthickness=0)
            bar.pack(side="right", padx=int(14 * S))
            for w_ in (row, mark, name, badge, bar):
                w_.bind("<Button-1>", lambda e, n=n: pick(n))
            rows[n] = (row, mark, badge, bar)
        if not rows:
            tk.Label(self.host, text="Микрофоны не найдены.", font=F(17), fg=WARN, bg=INK).pack(anchor="w")

        def poll():
            ranked = sorted(peak, key=peak.get, reverse=True)
            hint = None
            if ranked and peak[ranked[0]] > 0.03 and (len(ranked) == 1 or peak[ranked[0]] > 1.8 * peak[ranked[1]]):
                hint = ranked[0]                  # один микрофон слышит заметно громче остальных: скорее всего, у рта пульт
            for n, (row, mark, badge, bar) in rows.items():
                sel = chosen[0] == n
                row.configure(highlightbackground=BRASS if sel else LINE, highlightthickness=2 if sel else 1)
                mark.delete("all")
                mark.create_oval(2, 2, 2 * ring + 2, 2 * ring + 2, outline=BRASS if sel else MIST, width=int(2 * S))
                if sel:
                    mark.create_oval(ring - int(5 * S) + 2, ring - int(5 * S) + 2, ring + int(5 * S) + 2, ring + int(5 * S) + 2, fill=BRASS, outline="")
                badge.configure(text="слышит громче, похоже пульт" if n == hint and not sel else ("выбран" if sel else ""),
                                fg=OK if n == hint else BRASS)
                bar.delete("all")
                w = int(170 * S) * min(1.0, (level.get(n, 0) * 14) ** 0.6)
                bar.create_rectangle(0, 0, w, int(10 * S), fill=OK if sel else BRASS, width=0)
            self.after_job = self.after(100, poll)

        poll()

    # ---------- шаг 3: проверка ----------
    def s_test(self):
        self.title_label("Проверим", "Нажмите кнопку записи на пульте ещё раз. Должна появиться зелёная надпись.", 3)
        card = self.card()
        card.pack(fill="x", pady=int(14 * S))
        status = tk.Label(card, text="Жду нажатия…", font=F(24, True), fg=WARN, bg=PANEL)
        status.pack(anchor="w", padx=int(20 * S), pady=int(22 * S))
        nxt = self.nav(back=lambda: self.show(2), nxt=lambda: self.show(4), nxt_enabled=False, skip=lambda: self.show(4))
        c = self.cand or dict(kind="hid", vid=0x4842, pid=0x0001, usage_page=0x0C, report=[3, 0xCF, 0],
                              label="G10S Pro, кнопка микрофона")     # шаг 1 пропущен: проверяем стандартную кнопку
        hidl = HidListener(only=(c["vid"], c["pid"], c["usage_page"])) if c.get("kind") == "hid" else None
        cw = v.ClickWatcher(remote_tag=None) if c.get("kind") == "key" else None
        self.cleanup = (hidl.close if hidl else (lambda: None))
        seen = [0]

        def poll():
            got = False
            if hidl:
                try:
                    while True:
                        info, data = hidl.q.get_nowait()
                        if list(data[:len(c["report"])]) == c["report"]:
                            got = True
                except queue.Empty:
                    pass
            if cw:
                items = list(cw.raw)
                got = any(e[1] == c["vk"] for e in items[seen[0]:])
                seen[0] = len(items)
            if got:
                status.configure(text="Работает!", fg=OK)
                self.enable(nxt)
            self.after_job = self.after(120, poll)

        poll()

    # ---------- шаг 4: готово ----------
    def s_finish(self):
        self.title_label("Готово", "Сохраню настройки и запущу программу. Значок AirDictate появится рядом с часами.", 4)
        c = self.cand or {}
        card = self.card()
        card.pack(fill="x", pady=int(8 * S))
        for k, val in (("Кнопка записи", c.get("label", "микрофон на пульте G10S Pro (стандартная)")), ("Микрофон пульта", self.audio_name or "не выбран")):
            r = tk.Frame(card, bg=PANEL)
            r.pack(fill="x", padx=int(20 * S), pady=int(8 * S))
            tk.Label(r, text=k, font=F(14), fg=MIST, bg=PANEL, width=16, anchor="w").pack(side="left")
            tk.Label(r, text=val, font=F(17), fg=CREAM, bg=PANEL, anchor="w", wraplength=int(480 * S), justify="left").pack(side="left")
        tk.Checkbutton(self.host, text="Запускать вместе с Windows", variable=self.autostart, font=F(17), fg=CREAM, bg=INK,
                       selectcolor=PANEL, activebackground=INK, activeforeground=CREAM).pack(anchor="w", pady=int(10 * S))
        self.nav(back=lambda: self.show(3), nxt=self.save_and_exit, nxt_text="Сохранить и запустить")

    def save_and_exit(self):
        cfg = v.load_cfg()
        c = self.cand
        if self.use_default and not c:
            cfg.pop("trigger", None)                   # вернуть стандартную кнопку микрофона
            cfg["vid"], cfg["pid"] = 0x4842, 0x0001
        if c:
            if c["kind"] == "hid":
                cfg["trigger"] = {"type": "hid", "vid": c["vid"], "pid": c["pid"], "usage_page": c["usage_page"], "report": c["report"]}
            else:
                cfg["trigger"] = {"type": "key", "vk": c["vk"]}
            if c.get("vid") is not None:
                cfg["vid"], cfg["pid"] = c["vid"], c["pid"]
        if self.audio_name:
            cfg["audio_device"] = self.audio_name
        cfg["setup_done"] = True
        v.save_cfg(cfg)
        try:
            v.set_autostart(bool(self.autostart.get()))
        except Exception:
            pass
        self.cleanup()
        if v.FROZEN:
            subprocess.Popen([sys.executable], creationflags=0x08000000)    # сторож сам снимет прежний stopped.flag
        else:
            subprocess.Popen(["cmd", "/c", os.path.join(HERE, "start.bat")], creationflags=0x08000000)
        self.destroy()


def main():
    if STOP_APP:
        stop_app()
    Wizard().mainloop()


if __name__ == "__main__":
    main()
