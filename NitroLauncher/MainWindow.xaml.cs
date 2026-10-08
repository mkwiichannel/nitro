using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;

namespace NitroLauncher;

public partial class MainWindow : Window
{
    JsonObject _state = new JsonObject();
    bool _playing;
    string _kind = "";
    bool _popupShown;
    bool _suppressLang;
    string _screen = "home";
    readonly DispatcherTimer _poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
    readonly DispatcherTimer _toastTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(9) };
    MiiWindow _mii;

    // CSS custom-property name -> (resource key, default colour). Same names the web design used.
    static readonly (string Css, string Res, string Def)[] PaletteMap =
    {
        ("--bg", "BgBrush", "#0a0512"),
        ("--bg-2", "Bg2Brush", "#120a1f"),
        ("--panel", "PanelBrush", "#1a0e2b"),
        ("--panel-hover", "PanelHoverBrush", "#241340"),
        ("--blue", "BlueBrush", "#ff8c00"),
        ("--blue-2", "Blue2Brush", "#ffa513"),
        ("--cyan", "CyanBrush", "#c084fc"),
        ("--text", "TextBrush", "#e8eefb"),
        ("--text-dim", "TextDimBrush", "#8b9ab3"),
        ("--text-faint", "TextFaintBrush", "#56637c"),
    };

    static readonly Dictionary<string, (string Title, string Info, string Button)> MiiText =
        new Dictionary<string, (string, string, string)>
        {
            ["en"] = ("Mii Editor", "Create and edit your Nitro Miis. They sync into Dolphin when you press Play.", "Open Mii editor"),
            ["de"] = ("Mii-Editor", "Erstelle und bearbeite deine Nitro-Miis. Sie werden beim Spielen mit Dolphin synchronisiert.", "Mii-Editor öffnen"),
            ["fr"] = ("Éditeur de Mii", "Crée et modifie tes Mii Nitro. Ils sont synchronisés avec Dolphin au lancement.", "Ouvrir l'éditeur de Mii"),
            ["es"] = ("Editor de Mii", "Crea y edita tus Mii de Nitro. Se sincronizan con Dolphin al pulsar Jugar.", "Abrir editor de Mii"),
        };

    public MainWindow()
    {
        InitializeComponent();

        _state = Backend.GetState();

        // language list
        foreach (var l in Tr.Languages) LangList.Items.Add(l.Name);

        // tabs / navigation
        TabHome.Click += (s, e) => ShowScreen("home");
        TabMii.Click += (s, e) => { ShowScreen("mii"); OpenMii(); };
        TabCredits.Click += (s, e) => ShowScreen("credits");
        TabSettings.Click += (s, e) => ShowScreen("settings");
        CardSettings.Click += (s, e) => ShowScreen("settings");
        CardCredits.Click += (s, e) => ShowScreen("credits");
        MiiBack.Click += (s, e) => ShowScreen("home");
        CreditsBack.Click += (s, e) => ShowScreen("home");
        SettingsBack.Click += (s, e) => ShowScreen("home");

        // play / close
        PlayBtn.Click += (s, e) => DoPlay();
        CardPlay.Click += (s, e) => DoPlay();
        CloseBtn.Click += (s, e) => Application.Current.Shutdown();

        // credits / mii
        DiscordBtn.Click += (s, e) => Backend.OpenDiscord();
        DiscordLink.Click += (s, e) => Backend.OpenDiscord();
        MiiOpenBtn.Click += (s, e) => OpenMii();

        // settings
        BrowseDolphin.Click += (s, e) => BrowseFile(SetDolphin, "Dolphin (*.exe)|*.exe|All files (*.*)|*.*");
        BrowseIso.Click += (s, e) => BrowseFile(SetIso, "Wii disc image (*.iso;*.wbfs;*.rvz;*.gcm;*.ciso)|*.iso;*.wbfs;*.rvz;*.gcm;*.ciso|All files (*.*)|*.*");
        BrowseModDir.Click += (s, e) => BrowseFolder(SetModDir);
        SaveBtn.Click += (s, e) => SaveSettings();
        LangBtn.Click += (s, e) =>
        {
            LangPopup.MinWidth = LangBtn.ActualWidth;
            LangPopup.IsOpen = !LangPopup.IsOpen;
        };
        LangList.SelectionChanged += (s, e) => OnLanguagePicked();

        // hero
        Hero.SizeChanged += (s, e) =>
        {
            Hero.Clip = new RectangleGeometry(new Rect(0, 0, Hero.ActualWidth, Hero.ActualHeight), 22, 22);
            HeroFade.Height = Math.Max(40, Hero.ActualHeight * 0.46);
        };
        SizeChanged += (s, e) => UpdateCompactLayout();

        _poll.Tick += (s, e) => PollProgress();
        _toastTimer.Tick += (s, e) => { _toastTimer.Stop(); NoticeBox.Visibility = Visibility.Collapsed; };

        ApplyState(_state);
        ShowScreen("home");
        UpdateCompactLayout();

        Backend.StateChanged += () => Dispatcher.BeginInvoke(new Action(ReloadTheme));
        Backend.StartBackgroundThemeLoop();

        Loaded += async (s, e) =>
        {
            await Task.Delay(600);
            RunBg<CheckResult>(() => Backend.CheckForUpdate(true),
                c =>
                {
                    ReloadTheme();
                    if (c != null && c.LauncherUpdateAvailable && c.LauncherDownloadUrl != "")
                        ShowLauncherPopup(c.LauncherDownloadUrl);
                },
                e2 => null);
        };
    }

    protected override void OnClosed(EventArgs e)
    {
        base.OnClosed(e);
        try { _mii?.Close(); } catch { }
    }

    // ------------------------------------------------------------ helpers
    void RunBg<T>(Func<T> work, Action<T> done, Func<Exception, T> onError)
    {
        Task.Run(() =>
        {
            T result;
            try { result = work(); }
            catch (Exception ex) { result = onError(ex); }
            Dispatcher.BeginInvoke(new Action(() => done(result)));
        });
    }

    static Color ParseColor(string hex)
    {
        try { return (Color)ColorConverter.ConvertFromString(hex); }
        catch { return Colors.Magenta; }
    }

    static bool IsHex(string v)
    {
        return !string.IsNullOrEmpty(v) && v.StartsWith("#") && (v.Length == 4 || v.Length == 7);
    }

    static BitmapImage LoadBitmap(string filePath, string resourceName)
    {
        try
        {
            var bi = new BitmapImage();
            bi.BeginInit();
            bi.CacheOption = BitmapCacheOption.OnLoad;
            if (!string.IsNullOrEmpty(filePath) && File.Exists(filePath)) bi.UriSource = new Uri(filePath);
            else bi.StreamSource = Res.Open(resourceName);
            bi.EndInit();
            bi.Freeze();
            return bi;
        }
        catch { return null; }
    }

    // ------------------------------------------------------------ theme
    void ApplyTheme(JsonObject colors)
    {
        var res = Application.Current.Resources;
        string blueHex = "#ff8c00";
        foreach (var (css, key, def) in PaletteMap)
        {
            string hex = def;
            try
            {
                if (colors != null && colors[css] != null)
                {
                    string v = colors[css].GetValue<string>();
                    if (IsHex(v)) hex = v;
                }
            }
            catch { }
            res[key] = new SolidColorBrush(ParseColor(hex));
            if (css == "--blue") blueHex = hex;
        }
        var b = ParseColor(blueHex);
        res["LineStrongBrush"] = new SolidColorBrush(Color.FromArgb(0x66, b.R, b.G, b.B));
        res["BlueSoftBrush"] = new SolidColorBrush(Color.FromArgb(0x1F, b.R, b.G, b.B));
        res["BlueFaintBrush"] = new SolidColorBrush(Color.FromArgb(0x0F, b.R, b.G, b.B));
    }

    void ApplyImages(JsonObject s)
    {
        var logo = LoadBitmap(s.Str("theme_logo_path"), "web/logo.png") ?? LoadBitmap(null, "web/logo.png");
        if (logo != null) LogoImg.Source = logo;
        var banner = LoadBitmap(s.Str("theme_banner_path"), "web/banner.png") ?? LoadBitmap(null, "web/banner.png");
        if (banner != null) BannerImg.Source = banner;
    }

    /// <summary>Background updater changed colors/logo/banner (or finished a modpack update).</summary>
    void ReloadTheme()
    {
        var s = Backend.GetState();
        _state = s;
        ApplyTheme(s.ObjOrNull("theme_colors"));
        ApplyImages(s);
        ApplyVersionInfo(s);
        ApplyStats(s);
    }

    // ------------------------------------------------------------ state -> UI
    void ApplyState(JsonObject s)
    {
        _state = s;
        string lang = s.Str("language");
        if (lang == "" || !Tr.Has(lang)) lang = "en";
        Tr.Lang = lang;
        Shell.FlowDirection = lang == "ar" ? FlowDirection.RightToLeft : FlowDirection.LeftToRight;

        ApplyTheme(s.ObjOrNull("theme_colors"));
        ApplyImages(s);
        ApplyVersionInfo(s);
        ApplyTexts();
        ApplyStats(s);

        SetDolphin.Text = s.Str("dolphin_path");
        SetIso.Text = s.Str("iso_path");
        SetModDir.Text = s.Str("mod_directory");
        SetRes.Text = s.Str("resolution");
        SetFull.IsChecked = s.Bool("fullscreen");
        SetAuto.IsChecked = s.Bool("auto_update", true);
        SelectLanguage(lang);
    }

    void SelectLanguage(string code)
    {
        _suppressLang = true;
        int idx = 0;
        for (int i = 0; i < Tr.Languages.Length; i++)
            if (Tr.Languages[i].Code == code) { idx = i; break; }
        LangList.SelectedIndex = idx;
        LangBtn.Content = Tr.Languages[idx].Name;
        _suppressLang = false;
    }

    void OnLanguagePicked()
    {
        if (_suppressLang) return;
        int i = LangList.SelectedIndex;
        if (i < 0 || i >= Tr.Languages.Length) return;
        string code = Tr.Languages[i].Code;
        LangBtn.Content = Tr.Languages[i].Name;
        LangPopup.IsOpen = false;
        if (!Tr.Has(code)) return;
        Tr.Lang = code;
        Shell.FlowDirection = code == "ar" ? FlowDirection.RightToLeft : FlowDirection.LeftToRight;
        ApplyTexts();
        ApplyStats(_state);
    }

    static string When(string iso)
    {
        if (DateTimeOffset.TryParse(iso, out var d)) return d.LocalDateTime.ToString("yyyy-MM-dd HH:mm");
        return "-";
    }

    void ApplyVersionInfo(JsonObject s)
    {
        VerText.Text = "v" + s.Str("version");
        VerPill.ToolTip = "Modpack updated: " + When(s.Str("modpack_updated_at")) +
                          "\nLauncher updated: " + When(s.Str("launcher_updated_at"));
    }

    void SetStat(TextBlock tb, string text, bool dim, string tooltip = null)
    {
        tb.Text = text;
        tb.ToolTip = string.IsNullOrEmpty(tooltip) ? null : tooltip;
        tb.SetResourceReference(TextBlock.ForegroundProperty, dim ? "TextFaintBrush" : "TextBrush");
    }

    void ApplyStats(JsonObject s)
    {
        string d = s.Str("dolphin_path");
        string i = s.Str("iso_path");
        SetStat(StatDolphin, d != "" ? Path.GetFileName(d) : Tr.T("notSet"), d == "", d);
        SetStat(StatIso, i != "" ? Path.GetFileName(i) : Tr.T("notSet"), i == "", i);
        string mod = s.Str("active_mod");
        SetStat(StatMod, mod != "" ? mod : Tr.T("none"), mod == "");
        SetStat(StatVersion, "v" + s.Str("version"), false);
    }

    void ApplyTexts()
    {
        string t(string k) => Tr.T(k);
        TabHome.Content = t("navHome");
        TabMii.Content = t("navMii");
        TabCredits.Content = t("navCredits");
        TabSettings.Content = t("navSettings");
        CloseBtn.Content = t("exitBtn");
        if (!_playing) PlayBtn.Content = "▶  " + t("playLabel");

        LabelDolphin.Text = t("labelDolphin").ToUpperInvariant();
        LabelIso.Text = t("labelIso").ToUpperInvariant();
        LabelMod.Text = t("labelActiveMod").ToUpperInvariant();
        LabelLauncher.Text = t("labelLauncher").ToUpperInvariant();

        CardPlayTitle.Text = t("cardPlayTitle");
        CardPlayDesc.Text = t("cardPlayDesc");
        CardSettingsTitle.Text = t("cardSettingsTitle");
        CardSettingsDesc.Text = t("cardSettingsDesc");
        CardCreditsTitle.Text = t("cardCreditsTitle");
        CardCreditsDesc.Text = t("cardCreditsDesc");

        var mt = MiiText.ContainsKey(Tr.Lang) ? MiiText[Tr.Lang] : MiiText["en"];
        MiiBack.Content = t("settingsBackBtn");
        MiiTitle.Text = "Mii";
        MiiTag.Text = "Nitro Mii library · syncs on Play";
        MiiInfo.Text = mt.Info;
        MiiOpenBtn.Content = mt.Button;

        SettingsBack.Content = t("settingsBackBtn");
        SettingsTitle.Text = t("settingsTitle");
        SettingsTag.Text = t("settingsTag");
        LblDolphinPath.Text = t("labelDolphinPath");
        LblIsoPath.Text = t("labelIsoPath");
        LblModDir.Text = t("labelModDir");
        LblRes.Text = t("labelResolution");
        LblLang.Text = t("labelLanguage");
        SetFull.Content = t("labelFullscreen");
        SetAuto.Content = t("labelAutoUpdate");
        SaveBtn.Content = t("saveSettingsLabel");
        BrowseDolphin.Content = t("browseLabel");
        BrowseIso.Content = t("browseLabel");
        BrowseModDir.Content = t("browseLabel");

        string ver = _state.Str("version");
        if (ver == "") ver = Backend.AppVersion;
        CreditsBack.Content = t("creditsBackBtn");
        CreditsTitle.Text = t("creditsTitle");
        CrHead1.Text = t("creditsAppName");
        CrText1.Text = t("creditsAppDesc").Replace("1.0.0", ver);
        CrHead2.Text = t("creditsLicenseTitle");
        CrText2.Text = t("creditsLicenseText");
        CrHead3.Text = t("creditsCommunityTitle");
        CrHead4.Text = t("creditsProfileTitle");
        CrText4.Text = t("creditsProfileText");
        DiscordBtnText.Text = t("openDiscordLabel");
    }

    void UpdateCompactLayout()
    {
        bool shortWin = ActualHeight < 780;
        double h = shortWin ? 92 : 127;
        foreach (var c in new[] { CardPlay, CardSettings, CardCredits }) c.Height = h;
        var v = shortWin ? Visibility.Collapsed : Visibility.Visible;
        CardPlayDesc.Visibility = v;
        CardSettingsDesc.Visibility = v;
        CardCreditsDesc.Visibility = v;
    }

    // ------------------------------------------------------------ navigation
    void ShowScreen(string key)
    {
        _screen = key;
        HomePage.Visibility = key == "home" ? Visibility.Visible : Visibility.Collapsed;
        MiiPage.Visibility = key == "mii" ? Visibility.Visible : Visibility.Collapsed;
        CreditsPage.Visibility = key == "credits" ? Visibility.Visible : Visibility.Collapsed;
        SettingsPage.Visibility = key == "settings" ? Visibility.Visible : Visibility.Collapsed;
        TabHome.IsChecked = key == "home";
        TabMii.IsChecked = key == "mii";
        TabCredits.IsChecked = key == "credits";
        TabSettings.IsChecked = key == "settings";
    }

    void OpenMii()
    {
        try
        {
            if (_mii != null) { _mii.Activate(); return; }
            _mii = new MiiWindow();
            _mii.Closed += (s, e) => _mii = null;
            _mii.Show();
        }
        catch (Exception ex)
        {
            Toast("Couldn't open the Mii editor: " + ex.Message, true);
        }
    }

    // ------------------------------------------------------------ toast
    void Toast(string text, bool error = false)
    {
        NoticeText.Text = text;
        if (error) NoticeText.Foreground = new SolidColorBrush(Color.FromRgb(255, 123, 123));
        else NoticeText.SetResourceReference(TextBlock.ForegroundProperty, "TextBrush");
        NoticeBox.Visibility = Visibility.Visible;
        _toastTimer.Stop();
        _toastTimer.Start();
    }

    // ------------------------------------------------------------ settings
    void BrowseFile(TextBox box, string filter)
    {
        var d = new OpenFileDialog { Filter = filter, CheckFileExists = true };
        try
        {
            if (box.Text != "") d.InitialDirectory = Path.GetDirectoryName(box.Text);
        }
        catch { }
        if (d.ShowDialog(this) == true) box.Text = d.FileName;
    }

    void BrowseFolder(TextBox box)
    {
        var d = new OpenFolderDialog();
        try { if (box.Text != "") d.InitialDirectory = box.Text; } catch { }
        if (d.ShowDialog(this) == true) box.Text = d.FolderName;
    }

    void SaveSettings()
    {
        int li = LangList.SelectedIndex;
        string code = li >= 0 && li < Tr.Languages.Length ? Tr.Languages[li].Code : "en";
        var payload = new JsonObject
        {
            ["dolphin_path"] = SetDolphin.Text,
            ["iso_path"] = SetIso.Text,
            ["mod_directory"] = SetModDir.Text,
            ["resolution"] = SetRes.Text,
            ["language"] = code,
            ["fullscreen"] = SetFull.IsChecked == true,
            ["auto_update"] = SetAuto.IsChecked == true,
        };
        Backend.SaveSettings(payload);
        ApplyState(Backend.GetState());
        Toast(Tr.T("toastSettingsSaved"));
    }

    // ------------------------------------------------------------ play flow
    void SetPlaying(bool busy, string text = null)
    {
        _playing = busy;
        PlayBtn.IsEnabled = !busy;
        CardPlay.IsEnabled = !busy;
        PlayBtn.Content = busy ? (text ?? "") : "▶  " + Tr.T("playLabel");
        if (!busy) Prog.Visibility = Visibility.Collapsed;
    }

    void DoPlay()
    {
        if (_playing) return;
        SetPlaying(true, Tr.T("checkingFiles"));
        RunBg<CheckResult>(() => Backend.CheckForUpdate(false), AfterCheck,
            ex => new CheckResult { Error = ex.Message });
    }

    void AfterCheck(CheckResult c)
    {
        if (c.LauncherUpdateAvailable && c.LauncherDownloadUrl != "")
            ShowLauncherPopup(c.LauncherDownloadUrl);

        if (c.UpdateAvailable)
        {
            try
            {
                Backend.StartUpdate(c.DownloadUrl, c.LatestVersion);
            }
            catch (Exception)
            {
                Toast(Tr.T("toastFailedStart"), true);
                SetPlaying(false);
                return;
            }
            _kind = Tr.T(c.IsFirstInstall ? "downloadingMod" : "downloadingUpdate");
            Toast(Tr.T(c.IsFirstInstall ? "toastDownloadingFirst" : "toastDownloadingUpdate") + " " + Tr.T("toastDontClose"));
            Prog.IsIndeterminate = true;
            Prog.Visibility = Visibility.Visible;
            _poll.Start();
            return;
        }
        if (!string.IsNullOrEmpty(c.Error))
            Toast($"{Tr.T("toastCouldntCheck")} ({c.Error})", true);
        Launch();
    }

    void PollProgress()
    {
        var p = Progress.Snapshot();
        switch (p.Status)
        {
            case "downloading":
                if (p.Total.HasValue && p.Total.Value > 0)
                {
                    Prog.IsIndeterminate = false;
                    Prog.Value = p.Downloaded * 1000.0 / p.Total.Value;
                    PlayBtn.Content = $"⬇ {_kind}… {(int)(p.Downloaded * 100 / p.Total.Value)}% ({p.Downloaded >> 20}/{p.Total.Value >> 20} MB)";
                }
                else
                {
                    Prog.IsIndeterminate = true;
                    PlayBtn.Content = $"⬇ {_kind}… {p.Downloaded >> 20} MB";
                }
                break;
            case "extracting":
                Prog.IsIndeterminate = true;
                PlayBtn.Content = Tr.T("extracting");
                break;
            case "done":
                _poll.Stop();
                Toast($"{Tr.T("toastGotPrefix")}{p.Version}. {Tr.T("launching")}");
                Launch();
                break;
            case "error":
                _poll.Stop();
                Toast(string.IsNullOrEmpty(p.Error) ? Tr.T("toastFailedFetch") : p.Error, true);
                SetPlaying(false);
                break;
        }
    }

    void Launch()
    {
        Prog.Visibility = Visibility.Collapsed;
        PlayBtn.Content = Tr.T("launching");
        RunBg<LaunchResult>(Game.Launch, AfterLaunch, ex => new LaunchResult { Ok = false, Error = ex.Message });
    }

    void AfterLaunch(LaunchResult r)
    {
        SetPlaying(false);
        if (r != null && r.Ok) Toast(string.IsNullOrEmpty(r.Message) ? Tr.T("toastLaunched") : r.Message);
        else Toast(r != null && !string.IsNullOrEmpty(r.Error) ? r.Error : Tr.T("toastCouldNotLaunch"), true);
    }

    // ------------------------------------------------------------ launcher self-update popup
    void ShowLauncherPopup(string url)
    {
        if (_popupShown) return;
        _popupShown = true;

        var win = new Window
        {
            Title = "Mario Kart Nitro",
            Owner = this,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
            SizeToContent = SizeToContent.WidthAndHeight,
            ResizeMode = ResizeMode.NoResize,
            ShowInTaskbar = false,
            FontFamily = (FontFamily)FindResource("FInter"),
        };
        win.SetResourceReference(BackgroundProperty, "PanelBrush");
        var panel = new StackPanel { Margin = new Thickness(32, 28, 32, 28), MinWidth = 320, MaxWidth = 420 };
        var msg = new TextBlock
        {
            Text = Tr.T("launcherUpdateMsg"),
            TextWrapping = TextWrapping.Wrap,
            FontSize = 15,
            Margin = new Thickness(0, 0, 0, 18),
            TextAlignment = TextAlignment.Center,
        };
        msg.SetResourceReference(TextBlock.ForegroundProperty, "TextBrush");
        var btn = new Button
        {
            Style = (Style)FindResource("PrimaryBtn"),
            Content = Tr.T("launcherUpdateBtn"),
            HorizontalAlignment = HorizontalAlignment.Center,
            MinWidth = 140,
        };
        btn.Click += (s, e) =>
        {
            btn.IsEnabled = false;
            msg.Text = Tr.T("launcherUpdateInstalling");
            Backend.StartLauncherUpdate(url,
                err => Dispatcher.BeginInvoke(new Action(() =>
                {
                    msg.Text = Tr.T("launcherUpdateFailed") + (string.IsNullOrEmpty(err) ? "" : "\n" + err);
                    btn.IsEnabled = true;
                })),
                () => Dispatcher.BeginInvoke(new Action(() => Application.Current.Shutdown())));
        };
        panel.Children.Add(msg);
        panel.Children.Add(btn);
        win.Content = panel;
        win.Show();
    }
}
