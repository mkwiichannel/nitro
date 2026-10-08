using System.Net;
using System.Net.Http;

namespace NitroLauncher;

public static class Net
{
    public const string BrowserUA =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
    public const string ToolUA = "MarioKartNitro-Launcher";

    public static readonly HttpClient Http = Make();

    static HttpClient Make()
    {
        var handler = new SocketsHttpHandler
        {
            AutomaticDecompression = DecompressionMethods.All,
            PooledConnectionLifetime = TimeSpan.FromMinutes(2),
            MaxConnectionsPerServer = 8,
            ConnectTimeout = TimeSpan.FromSeconds(20),
        };
        var c = new HttpClient(handler);
        c.Timeout = Timeout.InfiniteTimeSpan; // every call below has its own cancellation timeout
        return c;
    }

    public static HttpRequestMessage Get(string url, string ua)
    {
        var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.TryAddWithoutValidation("User-Agent", ua);
        return req;
    }

    /// <summary>Small download into memory (manifest, images).</summary>
    public static byte[] FetchBytes(string url, int timeoutSeconds, string ua = BrowserUA)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
        using var req = Get(url, ua);
        using var resp = Http.Send(req, HttpCompletionOption.ResponseContentRead, cts.Token);
        resp.EnsureSuccessStatusCode();
        using var s = resp.Content.ReadAsStream(cts.Token);
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        return ms.ToArray();
    }

    /// <summary>Reads at most <paramref name="max"/> bytes from the start of a file.</summary>
    public static byte[] FetchHead(string url, int max, int timeoutSeconds, string ua = ToolUA)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
        using var req = Get(url, ua);
        using var resp = Http.Send(req, HttpCompletionOption.ResponseHeadersRead, cts.Token);
        resp.EnsureSuccessStatusCode();
        using var s = resp.Content.ReadAsStream(cts.Token);
        var buf = new byte[max];
        int got = 0;
        while (got < max)
        {
            int n = s.Read(buf, got, max - got);
            if (n <= 0) break;
            got += n;
        }
        Array.Resize(ref buf, got);
        return buf;
    }

    /// <summary>Streams a (possibly huge) file to disk. progress(done, total-or-null).</summary>
    public static void DownloadFile(string url, string dest, Action<long, long?> progress)
    {
        using var req = Get(url, BrowserUA);
        using var resp = Http.Send(req, HttpCompletionOption.ResponseHeadersRead);
        resp.EnsureSuccessStatusCode();
        long? total = resp.Content.Headers.ContentLength;
        long done = 0;
        using (var s = resp.Content.ReadAsStream())
        using (var f = new FileStream(dest, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
        {
            var buf = new byte[1 << 20];
            while (true)
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(10)); // no data at all for 10 min
                int n = s.ReadAsync(buf, 0, buf.Length, cts.Token).GetAwaiter().GetResult();
                if (n <= 0) break;
                f.Write(buf, 0, n);
                done += n;
                progress?.Invoke(done, total);
            }
        }
        if (total.HasValue && new FileInfo(dest).Length != total.Value)
            throw new IOException("Download incomplete: the connection dropped partway through. Try again.");
    }
}
