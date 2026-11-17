; 星源智控 · 控制面板 安装包脚本
; 编译入口：installer\build_installer.ps1（版本从 VERSION 读，以 /DAppVersion 传入）
;   ISCC.exe /DAppVersion=2.1.1 cuktech_setup.iss
; 约束：版本号不在本文件里手写 —— 与 VERSION、exe FileVersion、程序 --version 同源。
; AppId 已定稿：固定不变，升级/覆盖安装靠它；改了会被当成另一个产品。

#ifndef AppVersion
  #define AppVersion "2.1.1"
#endif

#define SrcDir "..\dist\CuktechPanel"

[Setup]
AppId={{7C4E1F6A-2B9D-4E3A-9C11-5D2F8A6B0C43}
AppName=星源智控
AppVersion={#AppVersion}
AppVerName=星源智控 {#AppVersion}
AppPublisher=X.H 团队
; 安装向导的许可页：由 build_installer.ps1 从 ..\legal\用户协议.md 复制成纯文本
; （Inno 的 LicenseFile 只按 .txt/.rtf 读；直接改 md 会让标记符号上屏）。
LicenseFile=license.txt
DefaultDirName={localappdata}\Programs\CuktechPanel
DefaultGroupName=星源智控
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=CuktechPanel-{#AppVersion}-setup-win64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 每用户安装：固定装到 %LOCALAPPDATA%\Programs\CuktechPanel，不请求提权。
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\CuktechPanel.exe
UninstallDisplayName=星源智控 {#AppVersion}
; 用户数据不入安装清单：%LOCALAPPDATA%\CuktechPanel（配置/历史库）由应用自管，卸载保留；
; HKCU\Software\DoubaoWork\CuktechPanel 下的协议同意记录（legal/accepted、legal/accepted_at）
; 在卸载时删除，重装后回到首启确认；同键其他配置保留。
CloseApplications=yes

[Languages]
; 中文包随脚本放在 installer\Languages\，不进系统 Inno 目录；默认第一项即中文界面。
Name: "zh"; MessagesFile: "Languages\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 桌面快捷方式默认不建（复选框默认不勾），需要时在安装向导里勾选。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\星源智控"; Filename: "{app}\CuktechPanel.exe"
Name: "{group}\卸载 星源智控"; Filename: "{uninstallexe}"
Name: "{autodesktop}\星源智控"; Filename: "{app}\CuktechPanel.exe"; WorkingDir: "{app}"; Comment: "星源智控 · 原生控制面板"; Tasks: desktopicon

[Run]
Filename: "{app}\CuktechPanel.exe"; Description: "启动星源智控"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载只清协议同意记录：重装后重新确认一次；历史库与其余配置不动。
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\Software\DoubaoWork\CuktechPanel\legal"" /v accepted /f"; Flags: runhidden; RunOnceId: "DelConsentAccepted"
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\Software\DoubaoWork\CuktechPanel\legal"" /v accepted_at /f"; Flags: runhidden; RunOnceId: "DelConsentAcceptedAt"
