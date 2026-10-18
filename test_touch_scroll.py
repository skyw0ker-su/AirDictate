"""Проверки жеста «как на телефоне» без Windows и без пульта: подменяем ввод и часы.

    python -m unittest test_touch_scroll -v
"""
import unittest

import touch_scroll as ts
from touch_scroll import WM_LBUTTONDOWN as DOWN, WM_LBUTTONUP as UP, WM_MOUSEMOVE as MOVE


class FakeEnv:
    def __init__(self):
        self.remote, self.take, self.notch = True, True, 100.0
        self.calls = []
        self.raise_on_remote = False

    def from_remote(self):
        if self.raise_on_remote:
            raise RuntimeError("boom")
        return self.remote

    def can_take(self, x, y):
        return self.take

    def px_per_notch(self):
        return self.notch

    def wheel(self, d):
        self.calls.append(("wheel", d))

    def button(self, down):
        self.calls.append(("button", down))

    def click(self):
        self.calls.append(("click",))

    def notify_click(self):
        self.calls.append(("notify",))

    def beep(self):
        self.calls.append(("beep",))

    def wheels(self):
        return [c[1] for c in self.calls if c[0] == "wheel"]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(**cfg):
    env, clk = FakeEnv(), Clock()
    logs = []
    t = ts.TouchScroll(env, cfg, log=lambda *a: logs.append(" ".join(str(x) for x in a)), clock=clk, start=False)
    t.logs = logs
    return t, env, clk


def step(t, dy, dx=0, anchor=(500, 500)):
    """Движение пульта: указатель стоит на якоре (мы глотаем движения), хук видит якорь плюс шаг."""
    return t.hook(MOVE, anchor[0] + dx, anchor[1] + dy)


class TapAndScroll(unittest.TestCase):
    def test_tap_is_swallowed_then_replayed_as_click(self):
        t, env, clk = make()
        self.assertTrue(t.hook(DOWN, 500, 500))
        self.assertEqual(t.state, ts.PENDING)
        clk.advance(0.1)
        self.assertTrue(t.hook(UP, 500, 500))
        t.process()
        self.assertEqual(env.calls, [("notify",), ("click",)])
        self.assertEqual(t.state, ts.IDLE)

    def test_jitter_below_threshold_is_swallowed_and_still_a_tap(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        self.assertTrue(step(t, 8))
        self.assertTrue(step(t, -5, dx=6))
        t.process()
        self.assertEqual(env.wheels(), [])
        clk.advance(0.1)
        t.hook(UP, 500, 500)
        t.process()
        self.assertEqual(env.calls, [("notify",), ("click",)])

    def test_drag_down_scrolls_page_down_like_a_finger(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        self.assertTrue(step(t, 10))
        self.assertEqual(t.state, ts.PENDING)
        self.assertTrue(step(t, 25))       # набрали 35 пикселей: порог пройден
        self.assertEqual(t.state, ts.SCROLL)
        t.process()
        self.assertEqual(env.wheels(), [42])   # 35 px = 0,35 деления = 42 из 120, вверх (положительное)
        self.assertTrue(step(t, 50))
        t.process()
        self.assertEqual(env.wheels(), [42, 60])
        self.assertNotIn(("click",), env.calls)
        self.assertNotIn(("notify",), env.calls)

    def test_drag_up_scrolls_the_other_way(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        step(t, -40)
        t.process()
        self.assertEqual(env.wheels(), [-48])

    def test_invert_and_speed(self):
        t, env, clk = make(touch_scroll_invert=True, touch_scroll_speed=2.0)
        t.hook(DOWN, 500, 500)
        step(t, 50)
        t.process()
        self.assertEqual(env.wheels(), [-120])

    def test_release_after_scroll_is_swallowed_without_click(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        step(t, 60)
        t.process()
        self.assertTrue(t.hook(UP, 500, 500))
        t.process()
        self.assertNotIn(("click",), env.calls)
        self.assertEqual(t.state, ts.IDLE)


class Fling(unittest.TestCase):
    def scrolled(self, **cfg):
        t, env, clk = make(**cfg)
        t.hook(DOWN, 500, 500)
        for _ in range(5):
            step(t, 20)
            clk.advance(0.01)
            t.process()
        return t, env, clk

    def test_fling_keeps_going_then_stops(self):
        t, env, clk = self.scrolled()
        before = len(env.wheels())
        t.hook(UP, 500, 500)
        t.process()
        self.assertGreater(t.fling_v, 0)
        for _ in range(600):
            clk.advance(0.01)
            t.process()
        self.assertEqual(t.fling_v, 0.0)
        after = env.wheels()[before:]
        self.assertGreater(len(after), 3)
        self.assertTrue(all(n > 0 for n in after))
        self.assertGreaterEqual(after[0], after[-1])   # замедляется

    def test_no_fling_if_hand_stopped_before_release(self):
        t, env, clk = self.scrolled()
        clk.advance(0.5)
        t.hook(UP, 500, 500)
        t.process()
        self.assertEqual(t.fling_v, 0.0)

    def test_no_fling_when_disabled(self):
        t, env, clk = self.scrolled(touch_scroll_inertia=False)
        t.hook(UP, 500, 500)
        t.process()
        self.assertEqual(t.fling_v, 0.0)

    def test_slow_drag_does_not_fling(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        step(t, 40)
        t.process()
        for _ in range(3):
            clk.advance(0.2)
            step(t, 4)
            t.process()
        t.hook(UP, 500, 500)
        t.process()
        self.assertEqual(t.fling_v, 0.0)

    def test_touch_stops_fling(self):
        t, env, clk = self.scrolled()
        t.hook(UP, 500, 500)
        t.process()
        self.assertGreater(t.fling_v, 0)
        t.hook(DOWN, 500, 500)
        self.assertEqual(t.fling_v, 0.0)


class LongPress(unittest.TestCase):
    def pressed(self, **cfg):
        t, env, clk = make(**cfg)
        t.hook(DOWN, 500, 500)
        clk.advance(0.41)
        t.process()
        return t, env, clk

    def test_hold_without_moving_becomes_a_real_press(self):
        t, env, clk = self.pressed()
        self.assertEqual(env.calls, [("notify",), ("beep",), ("button", True)])
        self.assertEqual(t.state, ts.DRAG)
        self.assertTrue(t.pressed)

    def test_after_long_press_moves_pass_through_for_selection(self):
        t, env, clk = self.pressed()
        self.assertFalse(t.hook(MOVE, 540, 520))

    def test_release_is_replayed_after_the_press_in_order(self):
        t, env, clk = self.pressed()
        self.assertTrue(t.hook(UP, 540, 520))
        t.process()
        self.assertEqual(env.calls[-2:], [("button", True), ("button", False)])
        self.assertEqual(t.state, ts.IDLE)
        self.assertFalse(t.pressed)

    def test_moves_are_held_until_the_press_is_delivered(self):
        t, env, clk = make()
        t.state, t.pressed = ts.DRAG, False
        self.assertTrue(t.hook(MOVE, 540, 520))

    def test_beep_can_be_turned_off(self):
        t, env, clk = self.pressed(touch_beep=False)
        self.assertEqual(env.calls, [("notify",), ("button", True)])

    def test_long_press_time_is_configurable(self):
        t, env, clk = make(touch_long_press=1.0)
        t.hook(DOWN, 500, 500)
        clk.advance(0.5)
        t.process()
        self.assertEqual(t.state, ts.PENDING)
        clk.advance(0.6)
        t.process()
        self.assertEqual(t.state, ts.DRAG)

    def test_moving_before_the_timer_cancels_long_press(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        step(t, 40)
        clk.advance(0.6)
        t.process()
        self.assertEqual(t.state, ts.SCROLL)
        self.assertNotIn(("button", True), env.calls)

    def test_close_releases_a_held_button(self):
        t, env, clk = self.pressed()
        t.close()
        self.assertEqual(env.calls[-1], ("button", False))
        self.assertFalse(t.pressed)

    def test_new_press_while_dragging_releases_the_old_one(self):
        t, env, clk = self.pressed()
        t.hook(DOWN, 500, 500)
        t.process()
        self.assertIn(("button", False), env.calls)


class PassThrough(unittest.TestCase):
    def assert_untouched(self, t, env):
        self.assertFalse(t.hook(DOWN, 500, 500))
        self.assertFalse(t.hook(MOVE, 510, 510))
        self.assertFalse(t.hook(UP, 510, 510))
        t.process()
        self.assertEqual(env.calls, [])
        self.assertEqual(t.state, ts.IDLE)

    def test_not_from_remote(self):
        t, env, clk = make()
        env.remote = False
        self.assert_untouched(t, env)

    def test_title_bar_and_other_places_we_must_not_take(self):
        t, env, clk = make()
        env.take = False
        self.assert_untouched(t, env)

    def test_disabled_in_settings(self):
        t, env, clk = make(touch_scroll=False)
        self.assert_untouched(t, env)

    def test_errors_never_swallow_events(self):
        t, env, clk = make()
        env.raise_on_remote = True
        self.assert_untouched(t, env)
        self.assertTrue(t.logs)

    def test_stuck_gesture_is_reset(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        clk.advance(ts.STUCK_SECONDS + 1)
        t.process()
        self.assertEqual(t.state, ts.IDLE)
        self.assertTrue(any("завис" in x for x in t.logs))

    def test_second_down_resets_unfinished_gesture(self):
        t, env, clk = make()
        t.hook(DOWN, 500, 500)
        step(t, 40)
        self.assertEqual(t.state, ts.SCROLL)
        t.hook(DOWN, 500, 500)
        self.assertEqual(t.state, ts.PENDING)


if __name__ == "__main__":
    unittest.main()
