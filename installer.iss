; Установщик AirDictate (Inno Setup). Сборка: ISCC.exe installer.iss (после build.ps1).
; Ставит программу для текущего пользователя, без прав администратора.

#define AppName "AirDictate"
#define AppVersion "1.0"
#define AppExe "AirDictate.exe"

[Setup]
AppId={{6F3B9C1E-2A7D-4B8E-9F10-AD1C0E5A7B42}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Скайвокер (Skyw0ker)
AppPublisherURL=https://github.com/skyw0ker-su/AirDictate
AppSupportURL=https://github.com/skyw0ker-su/AirDictate/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename={#AppName}-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=LICENSE
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked
Name: "autostart"; Description: "Запускать вместе с Windows"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "dist\AirDictate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Сначала останавливаем программу и убираем задачу-сторож, иначе она поднимет программу снова.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""AirDictate watchdog"" /F"; Flags: runhidden; RunOnceId: "DelWatchdog"

[UninstallDelete]
; Файлы, которые программа создала сама: настройки, журнал, история отправок, флаг остановки.
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\voice_remote.log"
Type: files; Name: "{app}\sent_history.json"
Type: files; Name: "{app}\stopped.flag"
Type: files; Name: "{app}\crash.log"
Type: files; Name: "{userstartup}\MyFlyMouse Voice.lnk"
