"""Точка входа собранной версии AirDictate.exe.

  AirDictate.exe            сторож: запускает рабочий процесс и поднимает его после сбоя
  AirDictate.exe --app      рабочий процесс (значок в трее, пульт)
  AirDictate.exe --wizard   мастер настройки (--stop-app: остановить программу на время настройки)
  AirDictate.exe --watchdog вызывает задача Планировщика каждую минуту
  AirDictate.exe --check    самопроверка: окно Claude и кнопка диктовки (пишет в журнал)
"""
import sys

import voice_remote as v

if "--wizard" in sys.argv:
    import setup_wizard
    setup_wizard.main()
elif "--app" in sys.argv:
    v.run(v.load_cfg())
elif "--watchdog" in sys.argv:
    v.watchdog()
elif "--check" in sys.argv:
    v.self_check()
else:
    v.supervise()
