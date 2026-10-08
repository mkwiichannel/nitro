using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using System.Xml.Linq;
using Microsoft.Win32;

namespace NitroLauncher;

public class LaunchResult
{
    public bool Ok;
    public string Message = "";
    public string Error = "";
    public string Detail = "";
}

/// <summary>Finding Dolphin's folders, building the Riivolution preset and starting Dolphin.</summary>
public static class Game
{
    // ------------------------------------------------------------ Dolphin folders
    /// <summary>Real Dolphin portable-mode detection: a portable.txt marker file, or the
    /// registry flag. (A leftover 'User' folder alone does NOT mean portable.)</summary>
    static bool IsPortable(string dolphinDir)
    {
        if (File.Exists(Path.Combine(dolphinDir, "portable.txt"))) return true;
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(@"Software\Dolphin Emulator");
            var v = key?.GetValue("LocalUserConfig");
            if (v != null && Convert.ToString(v) == "1") return true;
        }
        catch { }
        return false;
    }

    /// <summary>Dolphin's actual User folder, same priority order as the old launcher
    /// (and WheelWizard): portable, registry UserConfigPath, Documents, AppData.</summary>
    public static string DolphinUserDir(string dolphinPath)
    {
        string dolphinDir = Path.GetDirectoryName(dolphinPath) ?? "";
        string portableUser = Path.Combine(dolphinDir, "User");
        // A Dolphin user folder that already holds the Mii database wins: the launcher also
        // passes it with -u, so reads, writes and the game all use the same data directory.
        if (Directory.Exists(portableUser)
            && File.Exists(Path.Combine(portableUser, "Wii", "shared2", "menu", "FaceLib", "RFL_DB.dat")))
            return portableUser;
        if (IsPortable(dolphinDir) && Directory.Exists(portableUser))
            return portableUser;

        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(@"Software\Dolphin Emulator");
            var v = key?.GetValue("UserConfigPath");
            string s = v == null ? "" : Convert.ToString(v);
            if (!string.IsNullOrEmpty(s))
            {
                string normalized = s.Replace('/', Path.DirectorySeparatorChar);
                if (Directory.Exists(normalized)) return normalized;
            }
        }
        catch { }

        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string documents = Path.Combine(home, "Documents", "Dolphin Emulator");
        if (Directory.Exists(documents)) return documents;

        string appdata = Environment.GetEnvironmentVariable("APPDATA");
        if (!string.IsNullOrEmpty(appdata))
        {
            string ad = Path.Combine(appdata, "Dolphin Emulator");
            if (Directory.Exists(ad)) return ad;
        }
        return portableUser;
    }

    public static string RiivolutionRoot(string dolphinPath)
    {
        return Path.Combine(DolphinUserDir(dolphinPath), "Load", "Riivolution");
    }

    /// <summary>Close a running Dolphin first (Windows can't overwrite open files, and a second
    /// launch may not pick up the new preset).</summary>
    public static void KillDolphin(string dolphinPath)
    {
        try
        {
            string exe = Path.GetFileName(dolphinPath);
            if (string.IsNullOrEmpty(exe)) return;
            var psi = new ProcessStartInfo("taskkill", "/F /IM \"" + exe + "\"")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var p = Process.Start(psi);
            if (p != null)
            {
                p.StandardOutput.ReadToEnd();
                p.StandardError.ReadToEnd();
                p.WaitForExit(5000);
            }
            Thread.Sleep(500);
        }
        catch { /* non-critical */ }
    }

    static void CopyDir(string src, string dest)
    {
        Directory.CreateDirectory(dest);
        foreach (var f in Directory.GetFiles(src))
            File.Copy(f, Path.Combine(dest, Path.GetFileName(f)), true);
        foreach (var d in Directory.GetDirectories(src))
            CopyDir(d, Path.Combine(dest, Path.GetFileName(d)));
    }

    static void CopyInto(string srcDir, string destDir)
    {
        if (Directory.Exists(destDir))
        {
            bool has = false;
            try { has = Directory.EnumerateFileSystemEntries(destDir).Any(); } catch { }
            if (has) return;
            try { Directory.Delete(destDir, false); }
            catch (Exception e) { throw new IOException($"couldn't clear stale empty folder at {destDir}: {e.Message}"); }
        }
        CopyDir(srcDir, destDir);
    }

    // ------------------------------------------------------------ Riivolution preset
    /// <summary>Pick a choice for every option: "From Pack" when there is one, else the first.
    /// Dolphin matches by option name and choice indices are 1-based (0 = disabled).</summary>
    static JsonArray ParseOptions(string xmlPath)
    {
        var doc = XDocument.Load(xmlPath);
        var result = new JsonArray();
        if (doc.Root == null) return result;
        foreach (var section in doc.Root.Elements("options").Elements("section"))
        {
            string sectionName = (string)section.Attribute("name") ?? "";
            foreach (var option in section.Elements("option"))
            {
                string optionName = (string)option.Attribute("name") ?? "";
                var choices = option.Elements("choice").ToList();
                if (choices.Count == 0) continue;
                int chosen = 1;
                if (choices.Count > 1)
                {
                    for (int i = 0; i < choices.Count; i++)
                    {
                        string cn = ((string)choices[i].Attribute("name") ?? "").ToLowerInvariant();
                        if (cn.Contains("pack")) { chosen = i + 1; break; }
                    }
                }
                result.Add(new JsonObject
                {
                    ["section-name"] = sectionName,
                    ["option-name"] = optionName,
                    ["choice"] = chosen,
                });
            }
        }
        return result;
    }

    static void WritePreset(string iso, string xml, string riivRoot, string displayName, string outPath)
    {
        var preset = new JsonObject
        {
            ["base-file"] = iso,
            ["display-name"] = displayName,
            ["riivolution"] = new JsonObject
            {
                ["patches"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["options"] = ParseOptions(xml),
                        ["root"] = riivRoot,
                        ["xml"] = xml,
                    },
                },
            },
            ["type"] = "dolphin-game-mod-descriptor",
            ["version"] = 1,
        };
        File.WriteAllText(outPath, preset.ToJsonString(J.Pretty), new UTF8Encoding(false));
    }

    /// <summary>Make Dolphin write its own log file (useful when something goes wrong).</summary>
    static void EnableDolphinFileLogging(string userDir)
    {
        try
        {
            string cfgDir = Path.Combine(userDir, "Config");
            Directory.CreateDirectory(cfgDir);
            string ini = Path.Combine(cfgDir, "Logger.ini");

            var sections = new List<(string name, List<KeyValuePair<string, string>> items)>();
            List<KeyValuePair<string, string>> Section(string name)
            {
                foreach (var s in sections) if (s.name == name) return s.items;
                var l = new List<KeyValuePair<string, string>>();
                sections.Add((name, l));
                return l;
            }
            void SetKey(string section, string key, string value)
            {
                var items = Section(section);
                for (int i = 0; i < items.Count; i++)
                    if (items[i].Key == key) { items[i] = new KeyValuePair<string, string>(key, value); return; }
                items.Add(new KeyValuePair<string, string>(key, value));
            }

            if (File.Exists(ini))
            {
                string cur = null;
                foreach (var raw in File.ReadAllLines(ini, Encoding.UTF8))
                {
                    string line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#")) continue;
                    if (line.StartsWith("[") && line.EndsWith("]"))
                    {
                        cur = line.Substring(1, line.Length - 2);
                        Section(cur);
                        continue;
                    }
                    int eq = line.IndexOf('=');
                    if (cur != null && eq > 0)
                        SetKey(cur, line.Substring(0, eq).Trim(), line.Substring(eq + 1).Trim());
                }
            }
            SetKey("Options", "WriteToFile", "True");
            SetKey("Options", "WriteToConsole", "True");
            SetKey("Options", "Verbosity", "4");
            foreach (var c in new[] { "CORE", "BOOT", "DISCIO", "IOS_FS", "FILEMON", "MASTER_LOG" })
                SetKey("Logs", c, "True");

            var sb = new StringBuilder();
            foreach (var s in sections)
            {
                sb.Append('[').Append(s.name).Append("]\n");
                foreach (var kv in s.items) sb.Append(kv.Key).Append(" = ").Append(kv.Value).Append('\n');
                sb.Append('\n');
            }
            File.WriteAllText(ini, sb.ToString(), new UTF8Encoding(false));
        }
        catch { /* non-critical */ }
    }

    // ------------------------------------------------------------ launch
    public static LaunchResult Launch()
    {
        var cfg = Cfg.Load();
        string dolphin = cfg.Str("dolphin_path");
        string iso = cfg.Str("iso_path");

        if (dolphin == "" || !File.Exists(dolphin))
            return Fail("Dolphin path isn't set (or the file doesn't exist). Set it in Settings first.");
        if (iso == "" || !File.Exists(iso))
            return Fail("MKW ISO path isn't set (or the file doesn't exist). Set it in Settings first.");

        KillDolphin(dolphin);

        // Flush Nitro-created/edited Miis into the same Dolphin user folder that is passed
        // to Dolphin with -u, before the game starts reading its NAND.
        var mii = MiiStore.SyncToDolphin();
        if (!mii.Ok)
            return Fail("Mii sync failed; Dolphin was not launched. " + (mii.Error ?? "Unknown Mii sync error."));

        string activeName = cfg.Str("active_mod");
        var mod = Cfg.ActiveMod(cfg);
        string launchTarget = iso;
        string note = "";
        string detail = "";
        string userDir = DolphinUserDir(dolphin);

        if (mod != null && mod.Str("xml_path") != "" && File.Exists(mod.Str("xml_path")))
        {
            string xmlPath = mod.Str("xml_path");
            string contentRoot = mod.Str("content_root");
            string riivRoot = null;

            if (contentRoot != "" && Directory.Exists(contentRoot))
            {
                try
                {
                    riivRoot = Path.Combine(userDir, "Load", "Riivolution");
                    Directory.CreateDirectory(riivRoot);
                    var linked = new List<string>();
                    // Modpack synced straight into Dolphin's folder (the normal case): already there.
                    bool sameRoot = string.Equals(
                        Path.GetFullPath(contentRoot).TrimEnd('\\', '/'),
                        Path.GetFullPath(riivRoot).TrimEnd('\\', '/'),
                        StringComparison.OrdinalIgnoreCase);
                    foreach (var srcDir in Directory.GetDirectories(contentRoot))
                    {
                        string entry = Path.GetFileName(srcDir);
                        if (!sameRoot) CopyInto(srcDir, Path.Combine(riivRoot, entry));
                        linked.Add(entry);
                    }

                    var existingXmls = new List<string>();
                    foreach (var f in Directory.EnumerateFiles(riivRoot, "*", SearchOption.AllDirectories))
                        if (f.EndsWith(".xml", StringComparison.OrdinalIgnoreCase)) { existingXmls.Add(f); }

                    if (sameRoot && File.Exists(xmlPath))
                    {
                        // the synced profile is already the one to use
                    }
                    else if (existingXmls.Count > 0)
                    {
                        xmlPath = existingXmls[0];
                    }
                    else
                    {
                        string xmlDir = Path.Combine(riivRoot, "riivolution");
                        Directory.CreateDirectory(xmlDir);
                        string staged = Path.Combine(xmlDir, Path.GetFileName(xmlPath));
                        File.Copy(xmlPath, staged, true);
                        xmlPath = staged;
                    }

                    var assetFolders = linked.Where(x => x != "riivolution").ToList();
                    if (assetFolders.Count == 0)
                        note = " ⚠ No asset folders found alongside riivolution — mod may not apply correctly.";
                    detail = $"Linked into {riivRoot}: [{string.Join(", ", linked)}] | Using xml: {xmlPath}";
                }
                catch (Exception e)
                {
                    riivRoot = null;
                    note = " ⚠ Couldn't link the content folder into Dolphin's folder.";
                    detail = "Link error: " + e.Message;
                }
            }
            else
            {
                // No content folder set: stage just the xml into Dolphin's own folder.
                try
                {
                    riivRoot = Path.Combine(userDir, "Load", "Riivolution");
                    string xmlDir = Path.Combine(riivRoot, "riivolution");
                    Directory.CreateDirectory(xmlDir);
                    string staged = Path.Combine(xmlDir, Path.GetFileName(xmlPath));
                    File.Copy(xmlPath, staged, true);
                    xmlPath = staged;
                    note = " ⚠ No content folder set — the mod may not fully apply.";
                    detail = $"Staged xml only at {staged}, no content_root set.";
                }
                catch (Exception e)
                {
                    riivRoot = null;
                    note = " ⚠ Couldn't stage the mod profile.";
                    detail = "Stage error: " + e.Message;
                }
            }

            if (riivRoot != null)
            {
                try
                {
                    Directory.CreateDirectory(Paths.Presets);
                    string presetPath = Path.Combine(Paths.Presets, activeName + ".json");
                    WritePreset(iso, xmlPath, riivRoot, "Mario Kart Wii — " + activeName, presetPath);
                    launchTarget = presetPath;
                }
                catch (Exception e)
                {
                    note = " ⚠ Couldn't build the mod preset, launching the plain ISO instead.";
                    detail = "Preset error: " + e.Message;
                    launchTarget = iso;
                }
            }
        }

        var psi = new ProcessStartInfo(dolphin) { UseShellExecute = false };
        psi.ArgumentList.Add("-e");
        psi.ArgumentList.Add(launchTarget);
        psi.ArgumentList.Add("-u");
        psi.ArgumentList.Add(userDir);
        psi.ArgumentList.Add("--config=Dolphin.Core.EnableCheats=False");
        psi.ArgumentList.Add("--config=Achievements.Achievements.Enabled=False");
        psi.ArgumentList.Add("--config=Graphics.Settings.HiresTextures=True");
        if (cfg.Bool("fullscreen"))
        {
            psi.ArgumentList.Add("--config=Dolphin.Display.Fullscreen=True");
            string res = cfg.Str("resolution").Trim();
            if (res != "") psi.ArgumentList.Add("--config=Dolphin.Display.FullscreenDisplayRes=" + res);
        }
        else
        {
            psi.ArgumentList.Add("--config=Dolphin.Display.Fullscreen=False");
        }
        EnableDolphinFileLogging(userDir);
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            return Fail("Couldn't launch Dolphin: " + e.Message);
        }

        return new LaunchResult
        {
            Ok = true,
            Message = "Launching..." + (note.Trim().StartsWith("⚠") ? note : ""),
            Detail = detail,
        };
    }

    static LaunchResult Fail(string error) => new LaunchResult { Ok = false, Error = error };
}
