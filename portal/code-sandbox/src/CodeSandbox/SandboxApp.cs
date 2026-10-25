using System.Diagnostics;
using System.Text.Json;
using Microsoft.Extensions.Options;

namespace CodeSandbox;

/// <summary>HTTP service <c>code-sandbox serve</c>: <c>GET /health</c> and <c>POST /compile</c> (portal-api.md §7.5.12).</summary>
internal static class SandboxApp
{
    /// <summary>Builds the application; <paramref name="configure"/> lets tests replace the server and settings.</summary>
    public static WebApplication Build(string[] args, Action<WebApplicationBuilder>? configure = null)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = args,
            ContentRootPath = AppContext.BaseDirectory,
        });
        builder.WebHost.ConfigureKestrel(kestrel => kestrel.AddServerHeader = false);
        builder.Services.Configure<SandboxOptions>(builder.Configuration.GetSection(SandboxOptions.Section));
        configure?.Invoke(builder);

        builder.Services.AddSingleton(provider => provider.GetRequiredService<IOptions<SandboxOptions>>().Value);
        builder.Services.AddSingleton(provider =>
        {
            var options = provider.GetRequiredService<SandboxOptions>();
            var (state, reason) = SdkState.Load(options.IndexDir, options.SdkDir);
            provider.GetRequiredService<ILoggerFactory>().CreateLogger("sdk")
                .LogInformation("sdk load status={Status} files={Files}", reason, state.References.Length);
            return state;
        });
        builder.Services.AddSingleton<CompileEndpoint>();

        var app = builder.Build();
        var referenceDir = app.Services.GetRequiredService<SandboxOptions>().ResolvedReferenceDir;
        if (!File.Exists(Path.Combine(referenceDir, "System.Runtime.dll")))
        {
            throw new InvalidOperationException("Reference assemblies of .NET 8 not found.");
        }

        app.Services.GetRequiredService<SdkState>();
        app.MapGet("/health", (SdkState sdk) => Results.Json(new HealthResponse(sdk.Version), Json.Options));
        app.MapPost("/compile", (HttpContext context, CompileEndpoint endpoint) => endpoint.HandleAsync(context));
        return app;
    }
}

/// <summary><c>POST /compile</c>: input checks, concurrency limit, time limit, numbers-only log.</summary>
internal sealed class CompileEndpoint(SdkState sdk, SandboxOptions options, ILogger<CompileEndpoint> logger)
{
    private readonly SemaphoreSlim _slots = new(options.MaxParallel, options.MaxParallel);

    /// <summary>Handles one request; a failure of one compilation never stops the service.</summary>
    public async Task<IResult> HandleAsync(HttpContext context)
    {
        var started = Stopwatch.GetTimestamp();
        if (sdk.Version is null)
        {
            return Refuse(StatusCodes.Status503ServiceUnavailable, new ErrorResponse("sdk_missing"), started);
        }

        if (context.Request.ContentLength > options.RequestMaxBytes)
        {
            return Refuse(StatusCodes.Status400BadRequest, new ErrorResponse("invalid_request", "Тело запроса больше предела."), started);
        }

        CompileRequest? body;
        try
        {
            using var buffer = new MemoryStream();
            if (!await CopyLimitedAsync(context.Request.Body, buffer, options.RequestMaxBytes, context.RequestAborted))
            {
                return Refuse(StatusCodes.Status400BadRequest, new ErrorResponse("invalid_request", "Тело запроса больше предела."), started);
            }

            buffer.Position = 0;
            body = JsonSerializer.Deserialize<CompileRequest>(buffer, Json.Options);
        }
        catch (JsonException)
        {
            return Refuse(StatusCodes.Status400BadRequest, new ErrorResponse("invalid_request", "Тело запроса — не JSON протокола."), started);
        }

        var (request, rejection) = RequestValidator.Validate(body, options);
        if (rejection is not null)
        {
            return Refuse(StatusCodes.Status400BadRequest, new ErrorResponse(rejection.Error, rejection.Message), started);
        }

        if (!_slots.Wait(0))
        {
            return Refuse(StatusCodes.Status429TooManyRequests, new ErrorResponse("busy"), started);
        }

        try
        {
            using var limit = CancellationTokenSource.CreateLinkedTokenSource(context.RequestAborted);
            limit.CancelAfter(TimeSpan.FromSeconds(options.TimeoutSeconds));
            CompileOutcome outcome;
            try
            {
                var result = await CompileWorker.RunAsync(options, request!, limit.Token);
                if (result.Outcome is null)
                {
                    var error = result.SdkMissing ? "sdk_missing" : "internal";
                    logger.LogError("compile status=worker_failed exit_code={ExitCode} duration_ms={DurationMs}", result.ExitCode, ElapsedMs(started));
                    return Results.Json(new ErrorResponse(error), Json.Options, statusCode: result.SdkMissing ? StatusCodes.Status503ServiceUnavailable : StatusCodes.Status500InternalServerError);
                }

                outcome = result.Outcome;
            }
            catch (OperationCanceledException) when (!context.RequestAborted.IsCancellationRequested)
            {
                outcome = new CompileOutcome(Compiler.Timeout(sdk.Version!), 0, 0, 0);
            }

            logger.LogInformation(
                "compile status={Status} files={Files} bytes={Bytes} packages={Packages} errors={Errors} warnings={Warnings} findings={Findings} duration_ms={DurationMs}",
                outcome.Response.Status,
                request!.Files.Count,
                request.SourceBytes,
                request.Packages.Count,
                outcome.Errors,
                outcome.Warnings,
                outcome.Findings,
                ElapsedMs(started));
            return Results.Json(outcome.Response, Json.Options);
        }
        catch (OperationCanceledException)
        {
            logger.LogInformation("compile status=aborted duration_ms={DurationMs}", ElapsedMs(started));
            return Results.Empty;
        }
        catch (Exception exception)
        {
            // Only the type: an exception message may quote the source code.
            logger.LogError("compile status=internal_error exception={Exception} duration_ms={DurationMs}", exception.GetType().FullName, ElapsedMs(started));
            return Results.Json(new ErrorResponse("internal"), Json.Options, statusCode: StatusCodes.Status500InternalServerError);
        }
        finally
        {
            _slots.Release();
        }
    }

    private IResult Refuse(int statusCode, ErrorResponse error, long started)
    {
        logger.LogInformation("compile rejected status={StatusCode} error={Error} duration_ms={DurationMs}", statusCode, error.Error, ElapsedMs(started));
        return Results.Json(error, Json.Options, statusCode: statusCode);
    }

    /// <summary>Copies at most <paramref name="limit"/> bytes; <c>false</c> — the body is longer.</summary>
    private static async Task<bool> CopyLimitedAsync(Stream source, Stream target, long limit, CancellationToken cancellationToken)
    {
        var chunk = new byte[81920];
        long total = 0;
        int read;
        while ((read = await source.ReadAsync(chunk, cancellationToken)) > 0)
        {
            total += read;
            if (total > limit)
            {
                return false;
            }

            target.Write(chunk, 0, read);
        }

        return true;
    }

    private static long ElapsedMs(long started) => (long)Stopwatch.GetElapsedTime(started).TotalMilliseconds;
}
