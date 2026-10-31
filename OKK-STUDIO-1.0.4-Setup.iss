[Setup]
#ifdef RELEASE_QA
AppId=OKKSTUDIO.ReleaseQA.1.0.4
AppName=OKK STUDIO Release QA
#else
AppId=OKKSTUDIO.1.0.0
AppName=OKK STUDIO
#endif
AppVersion=1.0.4
AppVerName=OKK STUDIO 1.0.4
AppPublisher=OKK STUDIO
AppPublisherURL=https://github.com/lianduan683/OKK-STUDIO-1.0.0
AppSupportURL=https://github.com/lianduan683/OKK-STUDIO-1.0.0/issues
DefaultDirName={localappdata}\Programs\OKK STUDIO
DefaultGroupName=OKK STUDIO
DisableProgramGroupPage=yes
DisableWelcomePage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=artifacts
#ifdef RELEASE_QA
OutputBaseFilename=OKK-STUDIO-1.0.4-QA-Setup
#else
OutputBaseFilename=OKK-STUDIO-1.0.4-Setup
#endif
SetupIconFile=source\assets\app_icon.ico
UninstallDisplayIcon={app}\OKK STUDIO.exe
UninstallDisplayName=OKK STUDIO 1.0.4
VersionInfoVersion=1.0.4.0
VersionInfoProductVersion=1.0.4
Compression=lzma2/fast
SolidCompression=yes
ArchiveExtraction=full
WizardStyle=modern
CloseApplications=no
RestartApplications=no
SetupLogging=yes
InfoBeforeFile=installer_info.txt

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 / Create a desktop shortcut"; GroupDescription: "快捷方式 / Shortcuts:"

[Files]
; The complete core is embedded. Only resource archives are external downloads.
Source: "dist\OKK STUDIO 1.0.4\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-01.zip"; DestName: "OKK-STUDIO-1.0.0-resources-01.zip"; DestDir: "{app}"; ExternalSize: 1610612735; Hash: "37c4c7513e420a318c749c49f419977310eff33b4f7604301f4f7e79b6ccea96"; Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs; Check: not UseLocalPayload
Source: "{param:PAYLOADDIR|}\OKK-STUDIO-1.0.0-resources-01.zip"; DestDir: "{app}"; ExternalSize: 1610612735; Hash: "37c4c7513e420a318c749c49f419977310eff33b4f7604301f4f7e79b6ccea96"; Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs; Check: UseLocalPayload
Source: "https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-02.zip"; DestName: "OKK-STUDIO-1.0.0-resources-02.zip"; DestDir: "{app}"; ExternalSize: 1610612735; Hash: "6df6e65bb26a6bfb9df095366564bcaf7f857074c3109957c6f4c937c9490b5e"; Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs; Check: not UseLocalPayload
Source: "{param:PAYLOADDIR|}\OKK-STUDIO-1.0.0-resources-02.zip"; DestDir: "{app}"; ExternalSize: 1610612735; Hash: "6df6e65bb26a6bfb9df095366564bcaf7f857074c3109957c6f4c937c9490b5e"; Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs; Check: UseLocalPayload
Source: "https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-03.zip"; DestName: "OKK-STUDIO-1.0.0-resources-03.zip"; DestDir: "{app}"; ExternalSize: 1610612687; Hash: "3c3018d23d0fb9c3cfe17494e4b717061eda9c8c56ce1114a627d436a6a8a85a"; Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs; Check: not UseLocalPayload
Source: "{param:PAYLOADDIR|}\OKK-STUDIO-1.0.0-resources-03.zip"; DestDir: "{app}"; ExternalSize: 1610612687; Hash: "3c3018d23d0fb9c3cfe17494e4b717061eda9c8c56ce1114a627d436a6a8a85a"; Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs; Check: UseLocalPayload
Source: "https://github.com/lianduan683/OKK-STUDIO-1.0.0/releases/download/v1.0.0/OKK-STUDIO-1.0.0-resources-04.zip"; DestName: "OKK-STUDIO-1.0.0-resources-04.zip"; DestDir: "{app}"; ExternalSize: 1570763478; Hash: "99e050eb91407ae5b988ae423f4b3a2220d445dc38898cdbe829908ec351cb06"; Flags: external download extractarchive ignoreversion recursesubdirs createallsubdirs; Check: not UseLocalPayload
Source: "{param:PAYLOADDIR|}\OKK-STUDIO-1.0.0-resources-04.zip"; DestDir: "{app}"; ExternalSize: 1570763478; Hash: "99e050eb91407ae5b988ae423f4b3a2220d445dc38898cdbe829908ec351cb06"; Flags: external extractarchive ignoreversion recursesubdirs createallsubdirs; Check: UseLocalPayload

[Icons]
#ifndef RELEASE_QA
Name: "{group}\OKK STUDIO"; Filename: "{app}\OKK STUDIO.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\OKK STUDIO"; Filename: "{app}\OKK STUDIO.exe"; WorkingDir: "{app}"; Tasks: desktopicon
#endif

[Run]
#ifndef RELEASE_QA
Filename: "{app}\OKK STUDIO.exe"; Description: "启动 OKK STUDIO 1.0.4 / Launch OKK STUDIO"; Flags: nowait postinstall skipifsilent unchecked
#endif

[Code]
function UseLocalPayload: Boolean;
begin
  Result := ExpandConstant('{param:PAYLOADDIR|}') <> '';
end;
