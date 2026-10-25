using CodeSandbox;

const string Usage = "usage: code-sandbox serve | code-sandbox index --sdk <dir> --out <dir> [--ref <dir>]";

switch (args.FirstOrDefault())
{
    case "serve":
        await SandboxApp.Build(args[1..]).RunAsync();
        return 0;
    case CompileWorker.Command:
        using (var input = Console.OpenStandardInput())
        using (var stdout = Console.OpenStandardOutput())
        {
            return CompileWorker.Run(input, stdout, Console.Error);
        }

    case "index":
        var options = ParseOptions(args[1..]);
        if (options is null || !options.TryGetValue("--sdk", out var sdk) || !options.TryGetValue("--out", out var output))
        {
            Console.Error.WriteLine(Usage);
            return 1;
        }

        var referenceDir = options.GetValueOrDefault("--ref") ?? new SandboxOptions().ResolvedReferenceDir;
        return SdkIndexer.Run(sdk, output, referenceDir, Console.Out, Console.Error);
    default:
        Console.Error.WriteLine(Usage);
        return 1;
}

static Dictionary<string, string>? ParseOptions(string[] arguments)
{
    if (arguments.Length % 2 != 0)
    {
        return null;
    }

    var result = new Dictionary<string, string>(StringComparer.Ordinal);
    for (var i = 0; i < arguments.Length; i += 2)
    {
        if (arguments[i] is not ("--sdk" or "--out" or "--ref") || !result.TryAdd(arguments[i], arguments[i + 1]))
        {
            return null;
        }
    }

    return result;
}
