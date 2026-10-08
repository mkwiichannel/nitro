using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NitroLauncher;

/// <summary>Incremental modpack sync from a public GitHub repo straight into
/// Dolphin's Load/Riivolution folder. Same algorithm and same modpack_state.json
/// format as the previous launcher (see the notes in each method).</summary>
public class SyncException : Exception
{
    public SyncException(string message) : base(message) { }
}

public class FileSpec
{
    public string Sha = "";
    public long Size;
}

public class FileEntry
{
    public long Size;
    public long MtimeNs;
    public string Sha = "";
}

public class RemoteInfo
{
    public string TreeSha = "";
    public string Etag = "";
    public Dictionary<string, FileSpec> Files = new Dictionary<string, FileSpec>();
}

public class SyncState
{
    public string Repo = "", Branch = "", Dest = "", TreeSha = "", Etag = "";
    public Dictionary<string, FileEntry> Files = new Dictionary<string, FileEntry>();
    public Dictionary<string, FileSpec> Remote;
}

public class DownloadItem
{
    public string Rel = "", Sha = "";
    public long Size;
}

public class Plan
{
    public List<DownloadItem> Download = new List<DownloadItem>();
    public List<string> Delete = new List<string>();
    public Dictionary<string, FileEntry> Ok = new Dictionary<string, FileEntry>();
}

public static class ModpackSync
{
    public const string ApiBase = "https://api.github.com";
    public const string RawBase = "https://raw.githubusercontent.com";
    public const string MediaBase = "https://media.githubusercontent.com/media";
    public const string DefaultRepo = "mkwiichannel/Nitropack";
    public const string DefaultBranch = "main";
    public const string LfsPrefix = "lfs:";

    // The Riivolution XML redirects the game's save into this folder: the
    // player's progress, not mod content. Installed if missing, never touched.
    static readonly string[] ProtectedPrefixes = { "riivolution/save/" };
    static readonly char[] BadChars = { '<', '>', ':', '"', '|', '?', '*', '\\' };

    static bool Protected(string rel)
    {
        foreach (var p in ProtectedPrefixes)
            if (rel.StartsWith(p, StringComparison.Ordinal)) return true;
        return false;
    }

    static bool SafeRel(string rel)
    {
        if (string.IsNullOrEmpty(rel) || rel.StartsWith("/")) return false;
        foreach (var part in rel.Split('/'))
        {
            if (part == "" || part == "." || part == "..") return false;
            if (part.EndsWith(" ") || part.EndsWith(".")) return false;
            if (part.IndexOfAny(BadChars) >= 0) return false;
        }
        return true;
    }

    static string Quote(string rel)
    {
        return string.Join("/", rel.Split('/').Select(Uri.EscapeDataString));
    }

    public static string LocalPath(string dest, string rel)
    {
        return Path.Combine(dest, rel.Replace('/', Path.DirectorySeparatorChar));
    }

    public static string NormDest(string dest)
    {
        return Path.GetFullPath(dest).TrimEnd('\\', '/').ToLowerInvariant();
    }

    static long MtimeNs(string path)
    {
        // Same number the old launcher stored (Windows FILETIME converted to unix nanoseconds).
        return (File.GetLastWriteTimeUtc(path).Ticks - 621355968000000000L) * 100L;
    }

    // ------------------------------------------------------------ state
    public static SyncState LoadState(string path)
    {
        var st = new SyncState();
        try
        {
            if (!File.Exists(path)) return st;
            var o = JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject;
            if (o == null) return st;
            st.Repo = o.Str("repo");
            st.Branch = o.Str("branch");
            st.Dest = o.Str("dest");
            st.TreeSha = o.Str("tree_sha");
            st.Etag = o.Str("etag");
            if (o["files"] is JsonObject f)
            {
                foreach (var kv in f)
                {
                    if (kv.Value is JsonArray a && a.Count == 3)
                        st.Files[kv.Key] = new FileEntry
                        {
                            Size = a[0].GetValue<long>(),
                            MtimeNs = a[1].GetValue<long>(),
                            Sha = a[2].GetValue<string>(),
                        };
                }
            }
            if (o["remote"] is JsonObject r)
            {
                st.Remote = new Dictionary<string, FileSpec>();
                foreach (var kv in r)
                {
                    if (kv.Value is JsonArray a && a.Count == 2)
                        st.Remote[kv.Key] = new FileSpec { Sha = a[0].GetValue<string>(), Size = a[1].GetValue<long>() };
                }
            }
        }
        catch { }
        return st;
    }

    static void SaveState(string path, string repo, string branch, string dest,
        Dictionary<string, FileEntry> files, RemoteInfo remoteOrNull)
    {
        var o = new JsonObject
        {
            ["repo"] = repo,
            ["branch"] = branch,
            ["dest"] = NormDest(dest),
        };
        var f = new JsonObject();
        foreach (var kv in files)
            f[kv.Key] = new JsonArray { kv.Value.Size, kv.Value.MtimeNs, kv.Value.Sha };
        o["files"] = f;
        if (remoteOrNull != null)
        {
            o["tree_sha"] = remoteOrNull.TreeSha;
            o["etag"] = remoteOrNull.Etag;
            var r = new JsonObject();
            foreach (var kv in remoteOrNull.Files)
                r[kv.Key] = new JsonArray { kv.Value.Sha, kv.Value.Size };
            o["remote"] = r;
        }
        string tmp = path + ".tmp";
        File.WriteAllText(tmp, o.ToJsonString(), new UTF8Encoding(false));
        File.Move(tmp, path, true);
    }

    // ------------------------------------------------------------ remote listing
    public static RemoteInfo FetchRemote(string repo, string branch, SyncState state)
    {
        string url = $"{ApiBase}/repos/{repo}/git/trees/{Uri.EscapeDataString(branch)}?recursive=1";
        bool same = state.Repo == repo && state.Branch == branch;
        using var req = Net.Get(url, Net.ToolUA);
        req.Headers.TryAddWithoutValidation("Accept", "application/vnd.github+json");
        if (same && !string.IsNullOrEmpty(state.Etag) && state.Remote != null)
            req.Headers.TryAddWithoutValidation("If-None-Match", state.Etag);

        HttpResponseMessage resp;
        string body;
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(25));
            resp = Net.Http.Send(req, HttpCompletionOption.ResponseContentRead, cts.Token);
            using var s = resp.Content.ReadAsStream(cts.Token);
            using var sr = new StreamReader(s, Encoding.UTF8);
            body = sr.ReadToEnd();
        }
        catch (Exception e)
        {
            throw new SyncException("Couldn't reach GitHub: " + e.Message);
        }

        using (resp)
        {
            if (resp.StatusCode == HttpStatusCode.NotModified && same && state.Remote != null)
            {
                return new RemoteInfo { TreeSha = state.TreeSha, Files = state.Remote, Etag = state.Etag };
            }
            int code = (int)resp.StatusCode;
            if (code == 403 || code == 429)
                throw new SyncException("GitHub is rate-limiting this connection right now. Try again in a few minutes.");
            if (code == 404)
                throw new SyncException($"Modpack repository {repo} ({branch}) was not found.");
            if (code < 200 || code > 299)
                throw new SyncException($"GitHub answered HTTP {code} while checking the modpack.");

            string etag = resp.Headers.ETag != null ? resp.Headers.ETag.ToString() : "";
            JsonObject data;
            try { data = JsonNode.Parse(body) as JsonObject; }
            catch (Exception e) { throw new SyncException("Couldn't read GitHub's answer: " + e.Message); }
            if (data == null) throw new SyncException("Couldn't read GitHub's answer.");
            if (data.Bool("truncated"))
                throw new SyncException("The modpack repository is too large to list in one request.");

            var info = new RemoteInfo { TreeSha = data.Str("sha"), Etag = etag };
            if (data["tree"] is JsonArray tree)
            {
                foreach (var node in tree)
                {
                    if (node is not JsonObject entry) continue;
                    string rel = entry.Str("path");
                    // Only files inside top-level folders are installed (the repo's
                    // folders ARE the modpack); loose root files like README are not.
                    if (entry.Str("type") == "blob" && rel.Contains('/') && SafeRel(rel)
                        && !rel.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
                    {
                        long size = 0;
                        try { if (entry["size"] != null) size = entry["size"].GetValue<long>(); } catch { }
                        info.Files[rel] = new FileSpec { Sha = entry.Str("sha"), Size = size };
                    }
                }
            }
            if (info.Files.Count == 0)
                throw new SyncException("The modpack repository has no folders to install.");
            return info;
        }
    }

    // ------------------------------------------------------------ Git LFS
    // Big files (e.g. the sound archive) live in Git LFS: the repo holds a ~130 byte
    // pointer, and raw.githubusercontent.com serves that pointer instead of the file.
    // Pointers are detected here and the real file comes from media.githubusercontent.com.
    static (string oid, long size)? ParsePointer(byte[] data)
    {
        string text;
        try { text = new UTF8Encoding(false, true).GetString(data); }
        catch { return null; }
        if (!text.StartsWith("version https://git-lfs.github.com/spec/v1")) return null;
        string oid = null;
        long size = -1;
        foreach (var raw in text.Split('\n'))
        {
            string line = raw.Trim('\r');
            if (line.StartsWith("oid sha256:")) oid = line.Substring("oid sha256:".Length).Trim();
            else if (line.StartsWith("size "))
            {
                if (!long.TryParse(line.Substring(5).Trim(), out size)) return null;
            }
        }
        if (oid != null && oid.Length == 64 && size >= 0) return (oid, size);
        return null;
    }

    public static void ResolveLfs(string repo, string branch, RemoteInfo remote)
    {
        var small = remote.Files.Where(kv => !kv.Value.Sha.StartsWith(LfsPrefix)
                                             && kv.Value.Size >= 100 && kv.Value.Size <= 200)
                                .Select(kv => kv.Key).ToList();
        if (small.Count == 0) return;
        var found = new Dictionary<string, FileSpec>();
        object lk = new object();
        RunWorkers(small, 4, rel =>
        {
            string url = $"{RawBase}/{repo}/{Uri.EscapeDataString(branch)}/{Quote(rel)}";
            try
            {
                var head = Net.FetchHead(url, 512, 20);
                var ptr = ParsePointer(head);
                if (ptr.HasValue)
                    lock (lk) found[rel] = new FileSpec { Sha = LfsPrefix + ptr.Value.oid, Size = ptr.Value.size };
            }
            catch { /* leave as-is; a later check tries again */ }
        });
        foreach (var kv in found) remote.Files[kv.Key] = kv.Value;
    }

    // ------------------------------------------------------------ hashing
    static string Hex(byte[] b) => Convert.ToHexString(b).ToLowerInvariant();

    static string GitBlobSha(string path, Action<int> onBytes)
    {
        long len = new FileInfo(path).Length;
        using var h = IncrementalHash.CreateHash(HashAlgorithmName.SHA1);
        h.AppendData(Encoding.ASCII.GetBytes("blob " + len + "\0"));
        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            1 << 16, FileOptions.SequentialScan);
        var buf = new byte[1 << 20];
        int n;
        while ((n = fs.Read(buf, 0, buf.Length)) > 0)
        {
            h.AppendData(buf, 0, n);
            onBytes(n);
        }
        return Hex(h.GetHashAndReset());
    }

    static string Sha256Lfs(string path, Action<int> onBytes)
    {
        using var h = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            1 << 16, FileOptions.SequentialScan);
        var buf = new byte[1 << 20];
        int n;
        while ((n = fs.Read(buf, 0, buf.Length)) > 0)
        {
            h.AppendData(buf, 0, n);
            onBytes(n);
        }
        return LfsPrefix + Hex(h.GetHashAndReset());
    }

    /// <summary>Run body(item) on a few below-normal-priority threads, so hashing and
    /// downloading never make the window (or a small CPU) feel sluggish.</summary>
    static void RunWorkers<T>(IReadOnlyList<T> items, int workers, Action<T> body)
    {
        int next = -1;
        var threads = new List<Thread>();
        int count = Math.Min(workers, items.Count);
        for (int i = 0; i < count; i++)
        {
            var t = new Thread(() =>
            {
                try { Thread.CurrentThread.Priority = ThreadPriority.BelowNormal; } catch { }
                while (true)
                {
                    int idx = Interlocked.Increment(ref next);
                    if (idx >= items.Count) break;
                    try { body(items[idx]); } catch { }
                }
            });
            t.IsBackground = true;
            t.Start();
            threads.Add(t);
        }
        foreach (var t in threads) t.Join();
    }

    // ------------------------------------------------------------ planning
    /// <summary>Compare Dolphin's folder with the repo listing. Missing files and wrong sizes
    /// are found with plain stat calls; a file verified earlier with unchanged size and modified
    /// time is trusted without being read; anything new or changed is hashed.</summary>
    public static Plan MakePlan(string dest, Dictionary<string, FileSpec> remoteFiles, SyncState state,
        Action<long, long> progress)
    {
        string norm = NormDest(dest);
        var prev = state.Dest == norm ? state.Files : new Dictionary<string, FileEntry>();

        var plan = new Plan();
        var toHash = new List<string>();
        foreach (var kv in remoteFiles)
        {
            string rel = kv.Key;
            string sha = kv.Value.Sha;
            long size = kv.Value.Size;
            string p = LocalPath(dest, rel);
            FileInfo fi = new FileInfo(p);
            if (!fi.Exists)
            {
                plan.Download.Add(new DownloadItem { Rel = rel, Sha = sha, Size = size });
                continue;
            }
            if (Protected(rel)) continue; // exists: the player's data, never touched
            if (fi.Length != size)
            {
                plan.Download.Add(new DownloadItem { Rel = rel, Sha = sha, Size = size });
                continue;
            }
            if (prev.TryGetValue(rel, out var fp) && fp.Size == size && fp.MtimeNs == MtimeNs(p) && fp.Sha == sha)
                plan.Ok[rel] = new FileEntry { Size = fp.Size, MtimeNs = fp.MtimeNs, Sha = fp.Sha };
            else
                toHash.Add(rel);
        }

        long total = 0;
        foreach (var r in toHash) total += remoteFiles[r].Size;
        long done = 0;
        long lastTick = 0;
        object lk = new object();
        var okLocal = new Dictionary<string, FileEntry>();
        var bad = new List<string>();

        void OnBytes(int n)
        {
            lock (lk)
            {
                done += n;
                long now = Environment.TickCount64;
                if (progress != null && now - lastTick > 200)
                {
                    lastTick = now;
                    progress(done, total);
                }
            }
        }

        RunWorkers(toHash, 2, rel =>
        {
            var spec = remoteFiles[rel];
            string p = LocalPath(dest, rel);
            string actual = null;
            FileEntry entry = null;
            try
            {
                actual = spec.Sha.StartsWith(LfsPrefix) ? Sha256Lfs(p, OnBytes) : GitBlobSha(p, OnBytes);
                if (actual == spec.Sha)
                    entry = new FileEntry { Size = spec.Size, MtimeNs = MtimeNs(p), Sha = spec.Sha };
            }
            catch { }
            lock (lk)
            {
                if (entry != null) okLocal[rel] = entry;
                else bad.Add(rel);
            }
        });

        foreach (var kv in okLocal) plan.Ok[kv.Key] = kv.Value;
        foreach (var rel in bad)
            plan.Download.Add(new DownloadItem { Rel = rel, Sha = remoteFiles[rel].Sha, Size = remoteFiles[rel].Size });
        progress?.Invoke(total, total);

        foreach (var rel in prev.Keys)
            if (!remoteFiles.ContainsKey(rel) && !Protected(rel)) plan.Delete.Add(rel);
        return plan;
    }

    // ------------------------------------------------------------ download
    static long FreeSpace(string path)
    {
        string p = Path.GetFullPath(path);
        while (!string.IsNullOrEmpty(p) && !Directory.Exists(p))
        {
            string parent = Path.GetDirectoryName(p);
            if (string.IsNullOrEmpty(parent) || parent == p) break;
            p = parent;
        }
        return new DriveInfo(Path.GetPathRoot(Path.GetFullPath(string.IsNullOrEmpty(p) ? "." : p))).AvailableFreeSpace;
    }

    static FileEntry DownloadOne(string repo, string branch, string dest, DownloadItem item,
        Action<long> addBytes, CancellationToken stop)
    {
        bool lfs = item.Sha.StartsWith(LfsPrefix);
        string baseUrl = lfs ? MediaBase : RawBase;
        string url = $"{baseUrl}/{repo}/{Uri.EscapeDataString(branch)}/{Quote(item.Rel)}";
        string final = LocalPath(dest, item.Rel);
        Directory.CreateDirectory(Path.GetDirectoryName(final));
        string part = final + ".nitro-part";
        Exception lastErr = null;

        for (int attempt = 0; attempt < 4; attempt++)
        {
            stop.ThrowIfCancellationRequested();
            long got = 0;
            try
            {
                using var h = IncrementalHash.CreateHash(lfs ? HashAlgorithmName.SHA256 : HashAlgorithmName.SHA1);
                if (!lfs) h.AppendData(Encoding.ASCII.GetBytes("blob " + item.Size + "\0"));
                using (var req = Net.Get(url, Net.ToolUA))
                using (var resp = Net.Http.Send(req, HttpCompletionOption.ResponseHeadersRead, stop))
                {
                    resp.EnsureSuccessStatusCode();
                    using var s = resp.Content.ReadAsStream(stop);
                    using var f = new FileStream(part, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 16);
                    var buf = new byte[256 * 1024];
                    while (true)
                    {
                        using var cts = CancellationTokenSource.CreateLinkedTokenSource(stop);
                        cts.CancelAfter(TimeSpan.FromSeconds(60)); // 60 s without data = stalled
                        int n = s.ReadAsync(buf, 0, buf.Length, cts.Token).GetAwaiter().GetResult();
                        if (n <= 0) break;
                        f.Write(buf, 0, n);
                        h.AppendData(buf, 0, n);
                        got += n;
                        addBytes(n);
                    }
                }
                string digest = (lfs ? LfsPrefix : "") + Hex(h.GetHashAndReset());
                if (got != item.Size || digest != item.Sha)
                    throw new IOException($"checksum mismatch for {item.Rel} (repo changed while downloading?)");

                for (int swap = 0; swap < 8; swap++) // Dolphin/antivirus may briefly hold a file
                {
                    try
                    {
                        File.Move(part, final, true);
                        break;
                    }
                    catch (UnauthorizedAccessException)
                    {
                        if (swap == 7)
                            throw new SyncException($"Couldn't write {item.Rel}: the file is in use. Close Dolphin and try again.");
                        Thread.Sleep(500);
                    }
                    catch (IOException)
                    {
                        if (swap == 7)
                            throw new SyncException($"Couldn't write {item.Rel}: the file is in use. Close Dolphin and try again.");
                        Thread.Sleep(500);
                    }
                }
                return new FileEntry { Size = item.Size, MtimeNs = MtimeNs(final), Sha = item.Sha };
            }
            catch (SyncException) { throw; }
            catch (OperationCanceledException) when (stop.IsCancellationRequested) { throw new SyncException("cancelled"); }
            catch (Exception e)
            {
                lastErr = e;
                addBytes(-got);
                try { File.Delete(part); } catch { }
                Thread.Sleep(1500 * (attempt + 1));
            }
        }
        throw new SyncException($"Download failed for {item.Rel}: {(lastErr != null ? lastErr.Message : "unknown error")}");
    }

    /// <summary>Execute a plan from MakePlan. Throws SyncException on failure (everything finished so far
    /// stays valid and is remembered, so a retry carries on).</summary>
    public static void RunSync(string repo, string branch, string dest, RemoteInfo remote, Plan plan,
        SyncState state, string statePath, Action<long, long> progress, int workers = 4)
    {
        var todo = plan.Download;
        long total = 0;
        foreach (var d in todo) total += d.Size;
        if (todo.Count > 0)
        {
            long need = (long)(total * 1.02) + 64L * 1024 * 1024;
            long free = 0;
            try { free = FreeSpace(dest); } catch { free = long.MaxValue; }
            if (free < need)
                throw new SyncException($"Not enough free disk space for the modpack update (needs about {need >> 20} MB).");
        }
        Directory.CreateDirectory(dest);

        object lk = new object();
        long doneBytes = 0;
        long lastEmit = 0;
        var cts = new CancellationTokenSource();
        var files = new Dictionary<string, FileEntry>(plan.Ok);
        SyncException error = null;

        void AddBytes(long n)
        {
            lock (lk)
            {
                doneBytes += n;
                long now = Environment.TickCount64;
                if (progress != null && now - lastEmit > 200)
                {
                    lastEmit = now;
                    progress(Math.Max(0, doneBytes), total);
                }
            }
        }

        try
        {
            if (todo.Count > 0)
            {
                RunWorkers(todo, workers, item =>
                {
                    try
                    {
                        var entry = DownloadOne(repo, branch, dest, item, AddBytes, cts.Token);
                        lock (lk) files[item.Rel] = entry;
                    }
                    catch (SyncException e)
                    {
                        lock (lk) { if (error == null) { error = e; cts.Cancel(); } }
                    }
                    catch (Exception e)
                    {
                        lock (lk) { if (error == null) { error = new SyncException(e.Message); cts.Cancel(); } }
                    }
                });
                progress?.Invoke(error == null ? total : Math.Max(0, doneBytes), total);
            }
            if (error == null)
            {
                foreach (var rel in plan.Delete)
                {
                    try { File.Delete(LocalPath(dest, rel)); } catch { }
                    files.Remove(rel);
                    // prune now-empty folders, never above the destination
                    string d = Path.GetDirectoryName(LocalPath(dest, rel));
                    string root = Path.GetFullPath(dest).TrimEnd('\\', '/');
                    while (!string.IsNullOrEmpty(d) && Path.GetFullPath(d).TrimEnd('\\', '/') != root)
                    {
                        try { Directory.Delete(d, false); } catch { break; }
                        d = Path.GetDirectoryName(d);
                    }
                }
            }
        }
        finally
        {
            // Remember what's verified even after a failure, so a retry doesn't
            // have to re-hash everything it already finished.
            try { SaveState(statePath, repo, branch, dest, files, error == null ? remote : null); } catch { }
        }
        if (error != null) throw error;
    }

    /// <summary>Nothing to download: still store what was just verified, so the next check
    /// can skip reading those files again.</summary>
    public static void RememberVerified(string repo, string branch, string dest, RemoteInfo remote, Plan plan,
        SyncState state, string statePath)
    {
        var files = new Dictionary<string, FileEntry>(plan.Ok);
        foreach (var kv in state.Files)
            if (remote.Files.ContainsKey(kv.Key) && Protected(kv.Key)) files[kv.Key] = kv.Value;
        SaveState(statePath, repo, branch, dest, files, remote);
    }
}
