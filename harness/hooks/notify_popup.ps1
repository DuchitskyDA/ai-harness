<#
Окно-алерт владельцу поверх всех окон. Запускает notification.py
отсоединённо: powershell.exe -File notify_popup.ps1 -Title <b64> -Body <b64>
-Session <SHA-256 hex session_id> [-FocusPid <pid claude.exe> -FocusBorn <FILETIME>].
Windows PowerShell 5.1: WinForms + Add-Type C# 5.

Окно без рамки, TopMost, не в панели задач и БЕЗ захвата фокуса
(ShowWithoutActivation + WS_EX_NOACTIVATE + MA_NOACTIVATE). Живёт 5 секунд
(курсор над окном - до его ухода), до клика или до сигнала сессии (именованное
событие Local\ClaudeAlert-<session_id>: новый алерт той же сессии или
UserPromptSubmit из notification.py). Окно сессии
одно: владелец - мьютекс Local\ClaudeAlertOwner-<session_id>, новый ждёт его у
закрывающегося старого, при двух почти одновременных алертах остаётся последний.
Стопка в правом нижнем углу: слот - первый свободный именованный Mutex
Local\ClaudeAlertSlot<N>, N=0..3; слотов нет - окно не показывается (тост
остаётся в Центре уведомлений). Тост пишется только туда (SuppressPopup), баннер
даёт окно. Клик по окну закрывает его и ведёт на вкладку Windows Terminal этой
сессии: -FocusPython <python> -FocusScript <notify_focus.py> с --pid FocusPid
--born FocusBorn отдают HWND терминала, поднимает его этот процесс (право на
передний план - у получившего клик); цели нет или процесс сменился - окно просто
закрылось. Клик по крестику в правом верхнем углу
закрывает окно без перехода в терминал. Файл в UTF-8 с BOM: иначе PowerShell
5.1 читает кириллицу как ANSI. Любая ошибка глотается, код выхода всегда 0.
#>
param(
    [string]$Title = '',
    [string]$Body = '',
    [string]$Session = '',
    [string]$FocusPython = '',
    [string]$FocusScript = '',
    [int]$FocusPid = 0,
    [string]$FocusBorn = ''
)

$ErrorActionPreference = 'Stop'

$source = @'
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO.MemoryMappedFiles;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using System.Runtime.InteropServices;

// Единственность окна сессии. Владелец - именованный Mutex Local\ClaudeAlertOwner-<sid>
// (держит процесс, пока окно живо); Closed - событие "окно сессии закрыть" (новый алерт
// той же сессии, UserPromptSubmit из notify.py); поколение - 8 байт в именованной
// памяти Local\ClaudeAlertGen-<sid>: каждый пришедший пишет туда свой билет, побеждает
// последний, более старые ждущие уходят без окна. Новый ждёт мьютекс у закрывающегося
// старого и повторяет сигнал, пока не дождался, поэтому два почти одновременных алерта
// дают одно окно.
public class SessionLock : IDisposable
{
    const int OwnWaitSeconds = 8, RetryMs = 100;
    public readonly EventWaitHandle Closed;
    readonly Mutex owner;
    readonly MemoryMappedFile map;
    readonly MemoryMappedViewAccessor view;
    bool owned;

    SessionLock(string sid)
    {
        Closed = new EventWaitHandle(false, EventResetMode.ManualReset, "Local\\ClaudeAlert-" + sid);
        owner = new Mutex(false, "Local\\ClaudeAlertOwner-" + sid);
        map = MemoryMappedFile.CreateOrOpen("Local\\ClaudeAlertGen-" + sid, 8);
        view = map.CreateViewAccessor(0, 8);
    }

    // null - вытеснены более новым алертом сессии или старое окно не закрылось за 8 с
    public static SessionLock Acquire(string sid)
    {
        SessionLock s = new SessionLock(sid);
        long pidLow = System.Diagnostics.Process.GetCurrentProcess().Id & 0xFFFFL;
        long ticket = (DateTime.UtcNow.Ticks & ~0xFFFFL) + pidLow;  // порядок прихода, pid разводит равные метки
        s.view.Write(0L, ticket);
        DateTime until = DateTime.UtcNow.AddSeconds(OwnWaitSeconds);
        while (true)
        {
            bool got;
            try { got = s.owner.WaitOne(0); }
            catch (AbandonedMutexException) { got = true; }
            if (got) { s.owned = true; break; }
            s.Closed.Set();
            if (s.view.ReadInt64(0L) != ticket || DateTime.UtcNow > until) { s.Dispose(); return null; }
            Thread.Sleep(RetryMs);
        }
        if (s.view.ReadInt64(0L) != ticket) { s.Dispose(); return null; }
        s.Closed.Reset();
        return s;
    }

    public void Dispose()
    {
        if (owned) { try { owner.ReleaseMutex(); } catch { } owned = false; }
        owner.Dispose();
        Closed.Dispose();
        view.Dispose();
        map.Dispose();
    }
}

public class ClaudeAlertForm : Form
{
    const int W = 360, H = 90, Gap = 8, EdgePad = 12, SlotCount = 4;
    const int WS_EX_TOPMOST = 0x8, WS_EX_TOOLWINDOW = 0x80, WS_EX_NOACTIVATE = 0x08000000;
    const int WM_MOUSEACTIVATE = 0x21, MA_NOACTIVATE = 3;
    const int CloseSize = 22, ClosePad = 6;
    const int PollMs = 300, LifetimeSeconds = 5;

    [DllImport("user32.dll")]
    static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")]
    static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")]
    static extern bool IsWindow(IntPtr h);
    [DllImport("winmm.dll", CharSet = CharSet.Unicode)]
    static extern bool PlaySound(string file, IntPtr module, uint flags);
    const uint SND_ASYNC = 0x1, SND_NODEFAULT = 0x2, SND_FILENAME = 0x20000;

    static bool clicked;

    // мягкий двухтон вместо Asterisk (у владельца это громкий Windows Background);
    // файла нет или не сыграл - прежний системный звук
    static void PlayChime()
    {
        string wav = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                                            "Media\\Windows Notify Messaging.wav");
        if (System.IO.File.Exists(wav) && PlaySound(wav, IntPtr.Zero, SND_FILENAME | SND_ASYNC | SND_NODEFAULT))
            return;
        System.Media.SystemSounds.Asterisk.Play();
    }

    readonly string title, body;
    readonly SessionLock session;
    readonly Mutex slotMutex;
    readonly DateTime deadline = DateTime.UtcNow.AddSeconds(LifetimeSeconds);
    readonly float scale;
    readonly Font titleFont, bodyFont;
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();

    static readonly Color Back = Color.FromArgb(32, 33, 38);
    static readonly Color Edge = Color.FromArgb(72, 74, 84);
    static readonly Color Accent = Color.FromArgb(217, 119, 87);
    static readonly Color TitleColor = Color.FromArgb(245, 245, 247);
    static readonly Color BodyColor = Color.FromArgb(178, 181, 190);
    static readonly Color CloseHover = Color.FromArgb(58, 60, 70);

    bool hoverClose;

    Rectangle CloseRect
    {
        get { return new Rectangle(Width - S(ClosePad) - S(CloseSize), S(ClosePad), S(CloseSize), S(CloseSize)); }
    }

    ClaudeAlertForm(string title, string body, SessionLock session,
                    Mutex slotMutex, int slot, float scale)
    {
        this.title = title; this.body = body; this.session = session;
        this.slotMutex = slotMutex; this.scale = scale;
        titleFont = new Font("Segoe UI", 10f, FontStyle.Bold);
        bodyFont = new Font("Segoe UI", 9f);

        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        BackColor = Back;
        DoubleBuffered = true;
        Cursor = Cursors.Hand;
        ClientSize = new Size(S(W), S(H));

        Rectangle wa = Screen.PrimaryScreen.WorkingArea;
        int x = wa.Right - Width - S(EdgePad);
        int y = wa.Bottom - S(EdgePad) - (slot + 1) * Height - slot * S(Gap);
        if (y < wa.Top) y = wa.Top;
        Location = new Point(x, y);

        MouseUp += (s, e) => { clicked = !CloseRect.Contains(e.Location); Close(); };
        MouseMove += (s, e) =>
        {
            bool over = CloseRect.Contains(e.Location);
            if (over == hoverClose) return;
            hoverClose = over;
            Cursor = over ? Cursors.Default : Cursors.Hand;
            Invalidate(CloseRect);
        };
        MouseLeave += delegate
        {
            if (!hoverClose) return;
            hoverClose = false;
            Cursor = Cursors.Hand;
            Invalidate(CloseRect);
        };
        Shown += delegate { try { PlayChime(); } catch { } };
        timer.Interval = PollMs;
        timer.Tick += OnTick;
        timer.Start();
    }

    int S(int v) { return (int)Math.Round(v * scale); }

    protected override bool ShowWithoutActivation { get { return true; } }

    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams cp = base.CreateParams;
            cp.ExStyle |= WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW;
            return cp;
        }
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_MOUSEACTIVATE) { m.Result = (IntPtr)MA_NOACTIVATE; return; }
        base.WndProc(ref m);
    }

    void OnTick(object sender, EventArgs e)
    {
        if (session != null && session.Closed.WaitOne(0)) { Close(); return; }
        // срок вышел, но курсор над окном - ждём, пока уйдёт, чтобы окно не пропало из-под клика
        if (DateTime.UtcNow > deadline && !Bounds.Contains(Cursor.Position))
            Close();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        using (SolidBrush accent = new SolidBrush(Accent))
            g.FillRectangle(accent, 0, 0, S(4), Height);
        using (Pen edge = new Pen(Edge))
            g.DrawRectangle(edge, 0, 0, Width - 1, Height - 1);
        int left = S(16), width = Width - left - S(12);
        Rectangle cr = CloseRect;
        if (hoverClose)
            using (SolidBrush hot = new SolidBrush(CloseHover))
                g.FillRectangle(hot, cr);
        using (Pen cross = new Pen(hoverClose ? TitleColor : BodyColor, Math.Max(1f, 1.5f * scale)))
        {
            System.Drawing.Drawing2D.SmoothingMode oldMode = g.SmoothingMode;
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            int m = S(7);
            g.DrawLine(cross, cr.Left + m, cr.Top + m, cr.Right - m, cr.Bottom - m);
            g.DrawLine(cross, cr.Right - m, cr.Top + m, cr.Left + m, cr.Bottom - m);
            g.SmoothingMode = oldMode;
        }
        TextFormatFlags common = TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding
                               | TextFormatFlags.EndEllipsis | TextFormatFlags.Left;
        TextRenderer.DrawText(g, title, titleFont,
            new Rectangle(left, S(10), Width - left - S(CloseSize + ClosePad + 4), titleFont.Height),
            TitleColor, Back, common | TextFormatFlags.SingleLine);
        TextRenderer.DrawText(g, body, bodyFont,
            new Rectangle(left, S(10) + titleFont.Height + S(6), width, bodyFont.Height * 2),
            BodyColor, Back, common | TextFormatFlags.WordBreak);
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        timer.Stop();
        timer.Dispose();
        if (slotMutex != null)
        {
            try { slotMutex.ReleaseMutex(); } catch { }
            slotMutex.Dispose();
        }
        if (session != null) session.Dispose();
        titleFont.Dispose();
        bodyFont.Dispose();
        base.OnFormClosed(e);
    }

    static Mutex TakeSlot(out int slot)
    {
        for (int n = 0; n < SlotCount; n++)
        {
            Mutex m = new Mutex(false, "Local\\ClaudeAlertSlot" + n);
            bool got;
            try { got = m.WaitOne(0); }
            catch (AbandonedMutexException) { got = true; }
            if (got) { slot = n; return m; }
            m.Dispose();
        }
        slot = -1;
        return null;
    }

    static string CleanSession(string session)
    {
        return Regex.Replace(session ?? "", "[^0-9A-Za-z_-]", "");
    }

    // Клик: notify_focus.py выбирает вкладку сессии и печатает HWND окна терминала.
    // Внутри помощника не больше ~12 с (две попытки выбора), здесь терпение 25 с.
    static void FocusSession(string python, string script, int pid, string born)
    {
        if (!clicked || pid <= 0 || string.IsNullOrEmpty(python) || string.IsNullOrEmpty(script)) return;
        if (!Regex.IsMatch(born ?? "", "^[1-9][0-9]{0,19}$")) return;  // в командную строку - только число
        if (script.IndexOf('"') >= 0) return;  // путь идёт в командную строку в кавычках
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(python,
                "-X utf8 -B \"" + script + "\" --pid " + pid + " --born " + born);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            using (Process p = Process.Start(psi))
            {
                if (!p.WaitForExit(25000)) { try { p.Kill(); } catch { } return; }
                long h;
                if (!long.TryParse(p.StandardOutput.ReadToEnd().Trim(), out h) || h == 0) return;
                IntPtr hwnd = new IntPtr(h);
                if (!IsWindow(hwnd)) return;
                if (IsIconic(hwnd)) ShowWindow(hwnd, 9);  // SW_RESTORE
                SetForegroundWindow(hwnd);
            }
        }
        catch { }
    }

    public static void Run(string title, string body, string session, string focusPython, string focusScript,
                           int focusPid, string focusBorn)
    {
        try { SetProcessDPIAware(); } catch { }
        SessionLock lk = null;
        string sid = CleanSession(session);
        if (sid.Length > 0)
        {
            lk = SessionLock.Acquire(sid);
            if (lk == null) return;  // вытеснены более новым алертом этой сессии
        }
        int slot;
        Mutex mutex = TakeSlot(out slot);
        if (mutex == null)  // на экране уже 4 окна: не показываем
        {
            if (lk != null) lk.Dispose();
            return;
        }
        float scale;
        using (Graphics g = Graphics.FromHwnd(IntPtr.Zero)) scale = g.DpiX / 96f;
        Application.Run(new ClaudeAlertForm(title, body, lk, mutex, slot, scale));
        FocusSession(focusPython, focusScript, focusPid, focusBorn);
    }
}
'@

function ConvertFrom-B64([string]$text) {
    if (-not $text) { return '' }
    [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($text))
}

try {
    $titleText = ConvertFrom-B64 $Title
    $bodyText = ConvertFrom-B64 $Body

    # Тост только для истории: SuppressPopup кладёт его в Центр уведомлений без
    # баннера. Сбой тоста окно не отменяет.
    try {
        $aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' +
            [System.Security.SecurityElement]::Escape($titleText) + '</text><text>' +
            [System.Security.SecurityElement]::Escape($bodyText) + '</text></binding></visual></toast>')
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
        $toast.SuppressPopup = $true
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
    } catch { }

    Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing, System.Core -TypeDefinition $source -Language CSharp
    [ClaudeAlertForm]::Run($titleText, $bodyText, $Session, $FocusPython, $FocusScript, $FocusPid, $FocusBorn)
} catch { }
exit 0
