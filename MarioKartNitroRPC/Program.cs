using DiscordRPC;
using DiscordRPC.Logging;

class Program
{
    // YOUR DISCORD APPLICATION ID
    const string APPLICATION_ID = "1540841269553795112";

    // EXACT KEY OF YOUR DISCORD LARGE IMAGE
    const string IMAGE_KEY = "mariokart_nitro";

    static DiscordRpcClient client;

    static void Main()
    {
        client = new DiscordRpcClient(APPLICATION_ID);

        client.Logger = new ConsoleLogger()
        {
            Level = LogLevel.Warning
        };

        client.Initialize();

        client.SetPresence(new RichPresence()
        {
            Details = "Playing",
            State = "Mario Kart Nitro",

            Assets = new Assets()
            {
                LargeImageKey = IMAGE_KEY,
                LargeImageText = "Mario Kart Nitro"
            }
        });

        Console.WriteLine("Mario Kart Nitro Rich Presence is running!");
        Console.WriteLine("Press ENTER to close.");

        Console.ReadLine();

        client.Dispose();
    }
}
