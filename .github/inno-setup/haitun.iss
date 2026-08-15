; Inno Setup script for HaiTun Agent.
; Packages the entire haitun-workspace (including psi-agent.exe, copied in at build time).

#define MyAppName "HaiTun Agent"
#define MyAppVersion "1.0.3"
#define MyAppPublisher "Hefei Zhenzhi Artificial Intelligence Application Software Co., Ltd"
#define MyAppExeName "haitun.exe"

; 编译期护栏：拒绝把「缺失或明显是占位/桩」的主程序打进安装包。
; 真实 haitun.exe 由 build-haitun-launcher.ps1 编译产生（数十 KB 以上）；
; 若它不存在、或体积小到不可能是有效 PE（阈值 4 KB），直接编译报错，
; 避免产出「装完启动即报 216（无效可执行）」的坏安装包。
#ifndef SKIP_EXE_GUARD
  #if !FileExists("haitun.exe")
    #error 缺少 haitun.exe：请先运行 build-haitun-launcher.ps1 生成主程序启动器再编译安装包。
  #endif
  #if FileSize("haitun.exe") < 4096
    #error haitun.exe 体积过小（疑似占位/桩文件），拒绝打包。请用 build-haitun-launcher.ps1 生成真实启动器。
  #endif
#endif

[Setup]
AppId={{234DFAA2-39F9-4E4C-92C7-680728ADDA4A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\haitun.ico
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=HaiTun Agent Setup
SetupIconFile=haitun.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.LegalPageCaption=许可协议与隐私政策
chinesesimplified.LegalPageDescription=请阅读并同意以下协议后继续安装
chinesesimplified.LegalIntro=安装并使用 HaiTun Agent 前，请阅读以下两份文件。点击下方按钮可在浏览器中查看全文。%n%n只有勾选「我已阅读并同意」后，方可继续安装。
chinesesimplified.LegalViewTerms=查看《软件许可及服务协议》
chinesesimplified.LegalViewPrivacy=查看《隐私保护政策》
chinesesimplified.LegalAgree=我已阅读并同意《软件许可及服务协议》与《隐私保护政策》
chinesesimplified.LegalMustAgree=请先阅读并勾选同意《软件许可及服务协议》与《隐私保护政策》，然后才能继续安装。
chinesesimplified.LaunchBrokenExe=检测到主程序 haitun.exe 缺失或已损坏，安装可能不完整。请重新下载完整安装包后再安装；若问题依旧，请联系技术支持。
english.LegalPageCaption=License Agreement and Privacy Policy
english.LegalPageDescription=Please read and accept the following documents before installing
english.LegalIntro=Before installing and using HaiTun Agent, please read the two documents below. Click the buttons to view the full text in your browser.%n%nYou must check "I have read and agree" to continue.
english.LegalViewTerms=View the Software License and Service Agreement
english.LegalViewPrivacy=View the Privacy Policy
english.LegalAgree=I have read and agree to the Software License and Service Agreement and the Privacy Policy
english.LegalMustAgree=You must read and agree to the Software License and Service Agreement and the Privacy Policy before continuing.
english.LaunchBrokenExe=The main program haitun.exe is missing or corrupted; the installation may be incomplete. Please download the full installer again. If the problem persists, contact support.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\examples\haitun-workspace\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "haitun.ico"; DestDir: "{app}"
Source: "haitun.exe"; DestDir: "{app}"
; 协议文档：随程序安装一份到 {app}\legal，安装后仍可查阅
Source: "legal\service-agreement.html"; DestDir: "{app}\legal"; Flags: ignoreversion
Source: "legal\privacy-policy.html"; DestDir: "{app}\legal"; Flags: ignoreversion
; 同样两份带 dontcopy，供同意向导页在点击「查看」时释放到 {tmp} 用浏览器打开
Source: "legal\service-agreement.html"; Flags: dontcopy
Source: "legal\privacy-policy.html"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\haitun.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\haitun.ico"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: shellexec postinstall skipifsilent

[Code]
var
  LegalPage: TWizardPage;
  LegalAgreeCheck: TNewCheckBox;

// 点击「查看」按钮：把随包的 html 释放到 {tmp} 并用默认浏览器打开。
// ExtractTemporaryFile 的参数是 [Files] 里 dontcopy 项的文件名（不含路径）。
procedure OpenLegalDoc(const FileName: String);
var
  ResultCode: Integer;
begin
  ExtractTemporaryFile(FileName);
  ShellExec('open', ExpandConstant('{tmp}\' + FileName), '', '',
            SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

procedure ViewTermsClick(Sender: TObject);
begin
  OpenLegalDoc('service-agreement.html');
end;

procedure ViewPrivacyClick(Sender: TObject);
begin
  OpenLegalDoc('privacy-policy.html');
end;

{ 在欢迎页之后、目录选择页之前插入一个自定义同意页。 }
procedure InitializeWizard();
var
  Intro: TNewStaticText;
  BtnTerms, BtnPrivacy: TNewButton;
begin
  LegalPage := CreateCustomPage(wpWelcome,
    ExpandConstant('{cm:LegalPageCaption}'),
    ExpandConstant('{cm:LegalPageDescription}'));

  Intro := TNewStaticText.Create(LegalPage);
  Intro.Parent := LegalPage.Surface;
  Intro.AutoSize := False;
  Intro.WordWrap := True;
  Intro.Left := 0;
  Intro.Top := 0;
  Intro.Width := LegalPage.SurfaceWidth;
  Intro.Height := ScaleY(60);
  Intro.Caption := ExpandConstant('{cm:LegalIntro}');

  BtnTerms := TNewButton.Create(LegalPage);
  BtnTerms.Parent := LegalPage.Surface;
  BtnTerms.Left := 0;
  BtnTerms.Top := Intro.Top + Intro.Height + ScaleY(8);
  BtnTerms.Width := LegalPage.SurfaceWidth;
  BtnTerms.Height := ScaleY(26);
  BtnTerms.Caption := ExpandConstant('{cm:LegalViewTerms}');
  BtnTerms.OnClick := @ViewTermsClick;

  BtnPrivacy := TNewButton.Create(LegalPage);
  BtnPrivacy.Parent := LegalPage.Surface;
  BtnPrivacy.Left := 0;
  BtnPrivacy.Top := BtnTerms.Top + BtnTerms.Height + ScaleY(8);
  BtnPrivacy.Width := LegalPage.SurfaceWidth;
  BtnPrivacy.Height := ScaleY(26);
  BtnPrivacy.Caption := ExpandConstant('{cm:LegalViewPrivacy}');
  BtnPrivacy.OnClick := @ViewPrivacyClick;

  LegalAgreeCheck := TNewCheckBox.Create(LegalPage);
  LegalAgreeCheck.Parent := LegalPage.Surface;
  LegalAgreeCheck.Left := 0;
  LegalAgreeCheck.Top := BtnPrivacy.Top + BtnPrivacy.Height + ScaleY(18);
  LegalAgreeCheck.Width := LegalPage.SurfaceWidth;
  LegalAgreeCheck.Height := ScaleY(40);
  LegalAgreeCheck.Caption := ExpandConstant('{cm:LegalAgree}');
  LegalAgreeCheck.Checked := False;
end;

// 未勾选同意时，阻止离开同意页。
// 静默安装（/SILENT、/VERYSILENT，如自动更新链路）不显示向导，此时放行，
// 否则同意门禁会把静默更新卡死。交互安装才强制勾选。
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if WizardSilent() then
    Exit;
  if (LegalPage <> nil) and (CurPageID = LegalPage.ID) then
  begin
    if not LegalAgreeCheck.Checked then
    begin
      MsgBox(ExpandConstant('{cm:LegalMustAgree}'), mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  NeedsRestart := False;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM haitun.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM psi-agent.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// 运行期护栏：装完后校验主程序确实存在且体积合理（有效 PE 至少数十 KB，
// 占位/桩文件只有几字节）。万一打进来的 haitun.exe 缺失/损坏/是占位桩，
// 这里给出清晰中文提示，而不是让用户在启动时撞上系统的「216 无效可执行」。
// 用 Inno 内置 FileExists + FileSize，避免依赖 Pascal Script 里不稳的流 API。
function FileSize64Get(const Path: String): Int64;
var
  Size: Int64;
begin
  if FileSize64(Path, Size) then
    Result := Size
  else
    Result := 0;
end;

function ExeLooksValid(const Path: String): Boolean;
begin
  Result := FileExists(Path) and (FileSize64Get(Path) >= 4096);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExePath: String;
begin
  if CurStep = ssPostInstall then
  begin
    ExePath := ExpandConstant('{app}\' + '{#MyAppExeName}');
    if not ExeLooksValid(ExePath) then
      MsgBox(ExpandConstant('{cm:LaunchBrokenExe}'), mbError, MB_OK);
  end;
end;
