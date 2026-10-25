using System.Diagnostics;
using System.Text.Json;

namespace CodeSandbox;

/// <summary>Job of <c>code-sandbox compile-worker</c>: settings and the checked request, on stdin.</summary>
internal sealed record WorkerJob(SandboxOptions Options, ValidRequest Request);

/// <summary>Result of a worker run: the outcome, or the reason it is absent.</summary>
internal sealed record WorkerResult(CompileOutcome? Outcome, bool SdkMissing, int ExitCode);

/// <summary>
/// One compilation in its own process. Roslyn checks cancellation too rarely in some binding paths
/// (nested lambdas over overloads run for minutes after the token fired), so the time limit kills
/// the process tree: a hung compilation is removed and does not hold a slot (ПД §3.3, §8.5).
/// </summary>
internal static class CompileWorker
{
    /// <summary>Command of the worker process.</summary>
    public const string Command = "compile-worker";

    /// <summary>Exit code: the SDK no longer matches the manifest.</summary>
    public const int SdkMissingExitCode = 2;

    /// <summary>Runs the worker; cancellation (time limit or closed connection) kills it.</summary>
    public static async Task<WorkerResult> RunAsync(SandboxOptions options, ValidRequest request, CancellationToken cancellationToken)
    {
        var start = new ProcessStartInfo
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };
        var appHost = Path.Combine(AppContext.BaseDirectory, "code-sandbox");
        if (File.Exists(appHost))
        {
            start.FileName = appHost;
        }
        else
        {
            start.FileName = Environment.ProcessPath ?? "dotnet";
            start.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "code-sandbox.dll"));
        }

        start.ArgumentList.Add(Command);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Worker did not start.");
        try
        {
            var output = new MemoryStream();
            var reading = process.StandardOutput.BaseStream.CopyToAsync(output, cancellationToken);
            // stderr is drained and dropped: it may quote the source code.
            var draining = process.StandardError.BaseStream.CopyToAsync(Stream.Null, cancellationToken);
            await JsonSerializer.SerializeAsync(process.StandardInput.BaseStream, new WorkerJob(options, request), Json.Options, cancellationToken);
            process.StandardInput.Close();
            await process.WaitForExitAsync(cancellationToken);
            await Task.WhenAll(reading, draining);
            if (process.ExitCode != 0)
            {
                return new WorkerResult(null, process.ExitCode == SdkMissingExitCode, process.ExitCode);
            }

            output.Position = 0;
            return new WorkerResult(JsonSerializer.Deserialize<CompileOutcome>(output, Json.Options), false, 0);
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync(CancellationToken.None);
            }
        }
    }

    /// <summary>Body of the worker process: job from stdin, outcome to stdout; returns the exit code.</summary>
    public static int Run(Stream input, Stream output, TextWriter error)
    {
        try
        {
            var job = JsonSerializer.Deserialize<WorkerJob>(input, Json.Options) ?? throw new InvalidOperationException("Empty job.");
            var (sdk, _) = SdkState.Load(job.Options.IndexDir, job.Options.SdkDir);
            if (sdk.Version is null)
            {
                return SdkMissingExitCode;
            }

            var pack = ReferencePack.Load(job.Options.ResolvedReferenceDir);
            var outcome = new Compiler(pack, sdk, job.Options).Compile(job.Request, CancellationToken.None);
            JsonSerializer.Serialize(output, outcome, Json.Options);
            return 0;
        }
        catch (Exception exception)
        {
            // Only the type: an exception message may quote the source code.
            error.WriteLine(exception.GetType().FullName);
            return 1;
        }
    }
}
