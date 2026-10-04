; Zeitspur Installer (Inno Setup 6) - Installation pro Benutzer ohne Adminrechte.
; Kompilieren ueber build.ps1: Es gibt Programmordner und Versionsnummer mit (/DDistDir, /DMyAppVersion).

#define MyAppName "Zeitspur"
; Die Version steht nur in zeitspur/__init__.py - so tragen Setup und Programm immer dieselbe Nummer.
#ifndef MyAppVersion
  #error MyAppVersion fehlt - bitte ueber build.ps1 bauen (liest die Version aus zeitspur/__init__.py)
#endif
#define MyAppPublisher "Tim Schaller"
#define MyAppExeName "Zeitspur.exe"
#ifndef DistDir
  #define DistDir "..\dist\Zeitspur"
#endif
#define MutexName "Local\Zeitspur.Service"

[Setup]
AppId={{B1F0C3E2-6A5D-4F0B-9C0E-7D3A2F1E8C44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
; auch in den Dateieigenschaften der Setup.exe (Details -> Dateiversion)
VersionInfoVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Zeitspur
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ZeitspurSetup
SetupIconFile=..\assets\zeitspur.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
; Restart Manager schliessen lassen, was Dateien in {app} sperrt (Backstop zum Taskkill in PrepareToInstall).
; Wichtig, weil von Claude Desktop gestartete "Zeitspur.exe --mcp"-Helfer die EXE sonst sperren.
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Messages]
; Letzte Setup-Seite: sagen, was als Naechstes passiert und wo man Zeitspur spaeter wiederfindet.
FinishedLabel=Das Setup hat [name] auf Ihrem Computer installiert. Später öffnen Sie es über das Startmenü („Zeitspur“) oder über das Zeitspur-Symbol im Infobereich der Taskleiste.%n%nDer erste Start dauert einige Sekunden – ein kleines Startfenster zeigt währenddessen, dass Zeitspur lädt.

[Tasks]
Name: "autostart"; Description: "Zeitspur beim Anmelden automatisch starten (Tray-Symbol)"; GroupDescription: "Autostart:"
Name: "claudedesktop"; Description: "MCP-Server in Claude Desktop registrieren (claude_desktop_config.json wird ergänzt, Backup .bak)"; GroupDescription: "Claude-Integration:"; Flags: unchecked

[InstallDelete]
; Vor dem Kopieren die alten Programmbibliotheken entfernen. Sonst blieben Module liegen, die in der neuen
; Version fehlen - etwa die Standort-Historie nach einem Wechsel auf die Release-Ausgabe - und wuerden
; weiter geladen. Nutzerdaten liegen nicht hier, sondern in %LOCALAPPDATA%\Zeitspur.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Microsofts WebView2-Bootstrapper (tools\fetch_webview2.ps1): nur bei Bedarf entpackt und ausgefuehrt, nicht installiert
Source: "webview2\MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{group}\Zeitspur"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--show"; IconFilename: "{app}\{#MyAppExeName}"; Comment: "Zeitstrahl anzeigen (startet den Dienst, falls er nicht läuft)"
Name: "{group}\Zeitspur deinstallieren"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Zeitspur"; ValueData: """{app}\{#MyAppExeName}"" --autostart"; Flags: uninsdeletevalue; Tasks: autostart; Check: AutostartWanted

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--mcp --register-claude-desktop --quiet"; Flags: runhidden waituntilterminated skipifsilent; Tasks: claudedesktop; StatusMsg: "MCP-Server wird in Claude Desktop registriert …"
Filename: "{app}\{#MyAppExeName}"; Parameters: "--show"; Description: "Zeitspur jetzt starten (Ersteinrichtung)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Zeitspur\webview"

[Code]
const
  WebView2KeyLM = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2KeyCU = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2DownloadUrl = 'https://developer.microsoft.com/microsoft-edge/webview2/consumer/';

var
  WebView2Page: TOutputMarqueeProgressWizardPage;
  PrevInstallFound: Boolean;    { war Zeitspur vor diesem Setup schon installiert? }
  PrevAutostartFound: Boolean;  { und war der Autostart-Eintrag dabei gesetzt? }

function WebView2Installed: Boolean;
var
  pv: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, WebView2KeyLM, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then
    Result := True
  else if RegQueryStringValue(HKCU, WebView2KeyCU, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then
    Result := True;
end;

{ Stilles Update aus Zeitspur heraus: zeitspur/updater.py startet das Setup mit /UPDATE=1. }
function IsUpdateRun: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

{ Beendet ALLE laufenden Instanzen. Der Tray-Dienst wird sauber ueber "--quit" (benanntes Event) beendet;
  die von Claude Desktop gestarteten "--mcp"-Helfer halten keinen Mutex, sperren aber die EXE-Datei und
  werden daher zusaetzlich per taskkill beendet (Claude Desktop startet den Helfer bei Bedarf neu). }
function StopRunningInstance: Boolean;
var
  ResultCode, i: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\{#MyAppExeName}');
  if CheckForMutexes('{#MutexName}') and FileExists(Exe) then
  begin
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    for i := 0 to 30 do
    begin
      if not CheckForMutexes('{#MutexName}') then break;
      Sleep(500);
    end;
    { Den Mutex gibt Zeitspur erst frei, wenn die Datenbank zu ist - der Prozess braucht danach aber noch
      einen Moment bis zum Ende. Nicht mittendrin hart beenden. }
    Sleep(2000);
  end;
  { Verbliebene Prozesse hart beenden. Entscheidend: Die von Claude Desktop gestarteten "--mcp"-Helfer
    halten KEINEN Mutex, sperren aber die Programmdatei. Ein einzelnes taskkill kehrt zurueck, bevor sie
    wirklich beendet sind - die Mutex-Pruefung allein meldet dann faelschlich "frei" und das Ersetzen der
    EXE scheitert. taskkill liefert 128, sobald kein passender Prozess mehr existiert; darauf warten wir. }
  for i := 0 to 20 do
  begin
    if not Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName} /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Break;
    if ResultCode = 128 then
      Break;
    Sleep(400);
  end;
  Sleep(400);
  Result := not CheckForMutexes('{#MutexName}');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
end;

{ Millisekunden seit Systemstart - fuer die mitlaufende Zeitanzeige; Inno Setup bringt das nicht selbst mit. }
function GetTickCount: DWord;
  external 'GetTickCount@kernel32.dll stdcall';

procedure InitializeWizard;
begin
  WebView2Page := CreateOutputMarqueeProgressPage('Microsoft Edge WebView2-Runtime wird installiert',
    'Zeitspur braucht sie für das Programmfenster. Sie fehlt auf diesem PC und wird jetzt von Microsoft geladen.');
end;

{ Laesst Microsofts Bootstrapper im Hintergrund laufen und zeigt derweil Erklaerung, bewegten Balken und
  die verstrichene Zeit. Ein blockierendes Exec liesse die Anzeige minutenlang stillstehen - das sah im
  Sandbox-Test aus, als sei die Installation abgestuerzt. cmd schreibt nach dem Ende eine Markierungsdatei,
  die Schleife wartet darauf. }
function RunWebView2Bootstrapper: Boolean;
var
  ResultCode: Integer;
  Started: DWord;
  Bootstrapper, Marker, Params: String;
begin
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  Bootstrapper := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
  Marker := ExpandConstant('{tmp}\webview2.done');
  Params := '/v:on /c ""' + Bootstrapper + '" /silent /install & echo !errorlevel!> "' + Marker + '""';
  if WizardSilent or not Exec(ExpandConstant('{cmd}'), Params, '', SW_HIDE, ewNoWait, ResultCode) then
  begin
    { Ohne Oberflaeche (stille Installation) oder falls cmd gesperrt ist: einfach warten. }
    Result := Exec(Bootstrapper, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exit;
  end;
  WebView2Page.Show;
  try
    Started := GetTickCount;
    while not FileExists(Marker) and (GetTickCount - Started < 15 * 60 * 1000) do
    begin
      WebView2Page.SetText('Lädt die Runtime von Microsoft herunter und installiert sie. Das dauert meist 1 bis 2 Minuten.',
                           Format('Läuft seit %d Sekunden ...', [(GetTickCount - Started) div 1000]));
      WebView2Page.Animate;
      Sleep(100);
    end;
  finally
    WebView2Page.Hide;
  end;
  Result := FileExists(Marker);
end;

{ Fehlt die WebView2-Runtime (z. B. in der Windows Sandbox), wird Microsofts Bootstrapper mitinstalliert.
  Ohne Adminrechte installiert er die Runtime nur fuer diesen Benutzer - passend zu Zeitspur selbst.
  Er laedt die Runtime aus dem Netz; klappt das nicht, bekommt man einen Knopf zur Download-Seite. }
procedure InstallWebView2IfMissing;
var
  ResultCode: Integer;
begin
  if WebView2Installed then
    Exit;
  if not RunWebView2Bootstrapper then
    Log('WebView2-Bootstrapper: kein Abschluss erkannt');
  if WebView2Installed then
    Exit;
  if not WizardSilent then
    if MsgBox('Die Microsoft Edge WebView2-Runtime konnte nicht automatisch installiert werden - vermutlich fehlt die Internetverbindung.' + #13#10#13#10 +
              'Zeitspur wird trotzdem installiert, braucht die Runtime aber für das Programmfenster.' + #13#10#13#10 +
              'Download-Seite von Microsoft jetzt öffnen?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2DownloadUrl, '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

{ Autostart bei einer Neuinstallation gemaess Task setzen; bei einem Update nur dann, wenn er vorher
  aktiv war - sonst wuerde ein Update eine bewusste Abwahl in der App (oder im Task-Manager) ueberschreiben. }
function AutostartWanted: Boolean;
begin
  Result := (not PrevInstallFound) or PrevAutostartFound;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Existing: String;
begin
  Result := '';
  { Zustand VOR dem Kopieren der Dateien festhalten - danach existiert die EXE immer. }
  PrevInstallFound := FileExists(ExpandConstant('{app}\{#MyAppExeName}'));
  PrevAutostartFound := RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Zeitspur', Existing) and (Existing <> '');
  if not StopRunningInstance then
  begin
    Result := 'Zeitspur läuft noch und konnte nicht beendet werden. Bitte im Tray-Menü „Beenden“ wählen und das Setup erneut starten.';
    Exit;
  end;
  { Voraussetzungen gehoeren vor das Kopieren - so sieht man die WebView2-Installation als eigenen Schritt. }
  InstallWebView2IfMissing;
end;

{ Update aus Zeitspur heraus (/SILENT): Das kleine Fortschrittsfenster sagt, was passiert. Zeitspur ist in
  dieser Zeit weg - ohne Hinweis sah das im Sandbox-Test (04.10.2026) aus, als haette es sich selbst geloescht. }
procedure CurPageChanged(CurPageID: Integer);
begin
  if IsUpdateRun and ((CurPageID = wpPreparing) or (CurPageID = wpInstalling)) then
  begin
    WizardForm.Caption := 'Zeitspur wird aktualisiert';
    WizardForm.PageNameLabel.Caption := 'Zeitspur wird aktualisiert';
    WizardForm.PageDescriptionLabel.Caption := 'Das dauert meist unter einer Minute. Danach startet Zeitspur ' +
      'von selbst wieder – Einstellungen und Aufnahmen bleiben erhalten.';
  end;
end;

{ Nach einem stillen Update Zeitspur wieder starten - auch wenn das Setup abgebrochen ist, denn beendet
  hatte es Zeitspur da schon. Laeuft es bereits oder fehlt die Programmdatei, passiert nichts. }
procedure DeinitializeSetup;
var
  ResultCode: Integer;
  Exe: String;
begin
  if not IsUpdateRun then
    Exit;
  try
    Exe := ExpandConstant('{app}\{#MyAppExeName}');
  except
    Exit;   { das Setup ist abgebrochen, bevor der Programmordner feststand }
  end;
  if FileExists(Exe) and not CheckForMutexes('{#MutexName}') then
    Exec(Exe, '--autostart', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

function InitializeUninstall: Boolean;
begin
  Result := StopRunningInstance;
  if not Result then
    MsgBox('Zeitspur läuft noch und konnte nicht beendet werden. Bitte im Tray-Menü „Beenden“ wählen und die Deinstallation erneut starten.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Zeitspur');
    if DirExists(DataDir) then
      if MsgBox('Sollen auch die verschlüsselte Datenbank, der Schlüssel und die Konfiguration gelöscht werden?' + #13#10 +
                DataDir + #13#10#13#10 + 'Standard: Nein – die Daten bleiben erhalten und werden bei einer Neuinstallation weiterverwendet.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
