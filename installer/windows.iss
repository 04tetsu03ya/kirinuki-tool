; 切り抜き動画ツール Windows インストーラー（Inno Setup 6）
#define AppVer GetEnv("APP_VERSION")
#if AppVer == ""
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{9C2E4B71-3D58-4A0F-8E21-6B7D5A3C1F42}
AppName=切り抜き動画ツール
AppVersion={#AppVer}
AppPublisher=KirinukiTool
DefaultDirName={autopf}\KirinukiTool
DefaultGroupName=切り抜き動画ツール
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer_out
OutputBaseFilename=KirinukiTool-Setup-Windows
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\KirinukiTool.exe
UninstallDisplayName=切り抜き動画ツール
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\KirinukiTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\切り抜き動画ツール"; Filename: "{app}\KirinukiTool.exe"
Name: "{autodesktop}\切り抜き動画ツール"; Filename: "{app}\KirinukiTool.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KirinukiTool.exe"; Description: "{cm:LaunchProgram,切り抜き動画ツール}"; Flags: nowait postinstall skipifsilent
; 自動更新（静かなインストール）のあとは、アプリを自動で開き直す
Filename: "{app}\KirinukiTool.exe"; Flags: nowait skipifnotsilent
