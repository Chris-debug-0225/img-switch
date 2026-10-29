#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef AppExe
  #error AppExe is required
#endif
[Setup]
AppId={{AD6F616E-EE64-467A-8DD7-935BCB23A218}
AppName=IMG Switch
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\IMG Switch
DefaultGroupName=IMG Switch
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=IMG-Switch-{#AppVersion}-Windows-x64-unsigned-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\IMG-Switch.exe
[Files]
Source: "{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectLicense}"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "{#PythonLicense}"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "{#ThirdPartyLicenses}\*.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion
[Icons]
Name: "{autoprograms}\IMG Switch"; Filename: "{app}\IMG-Switch.exe"
[Run]
Filename: "{app}\IMG-Switch.exe"; Description: "Launch IMG Switch"; Flags: nowait postinstall skipifsilent
