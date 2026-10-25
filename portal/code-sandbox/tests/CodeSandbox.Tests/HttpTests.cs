using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;

namespace CodeSandbox.Tests;

/// <summary>Protocol of portal-api.md §7.5.12 through an in-memory server.</summary>
public sealed class HttpTests : IDisposable
{
    /// <summary>Nested lambdas over three overloads: binding grows as 3^depth, far beyond any time limit.</summary>
    private static readonly string SlowSource =
        "namespace Demo; public static class Slow { static int M(System.Func<int, int> f) => 0; static int M(System.Func<string, int> f) => 0; static int M(System.Func<long, int> f) => 0; "
        + "public static int F() => " + string.Concat(Enumerable.Range(0, 14).Select(i => $"M(a{i} => ")) + "0" + new string(')', 14) + "; }";

    private readonly TestSdk _sdk = TestSdk.Create();

    public void Dispose() => _sdk.Dispose();

    [Fact]
    public async Task HealthAndCompileWithSdk()
    {
        await using var app = await Start(_sdk.Options());
        var client = app.GetTestClient();

        var health = await client.GetStringAsync("/health");
        var response = await client.PostAsJsonAsync("/compile", Body([("A.cs", "namespace Demo; public class A { }")]));
        using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());

        Assert.Equal("{\"sdk_version\":\"stub\"}", health);
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("succeeded", json.RootElement.GetProperty("status").GetString());
        Assert.False(json.RootElement.GetProperty("diagnostics_truncated").GetBoolean());
        Assert.NotEmpty(json.RootElement.GetProperty("dll").GetBytesFromBase64());
        Assert.Equal(JsonValueKind.Array, json.RootElement.GetProperty("rules").GetProperty("findings").ValueKind);
        Assert.Equal(JsonValueKind.Array, json.RootElement.GetProperty("on_server").ValueKind);
    }

    [Fact]
    public async Task WithoutManifestHealthIsNullAndCompileIs503()
    {
        await using var app = await Start(_sdk.Options(options => options.IndexDir = Path.Combine(_sdk.Root, "none")));
        var client = app.GetTestClient();

        var health = await client.GetStringAsync("/health");
        var response = await client.PostAsJsonAsync("/compile", Body([("A.cs", "")]));

        Assert.Equal("{\"sdk_version\":null}", health);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Equal("{\"error\":\"sdk_missing\"}", await response.Content.ReadAsStringAsync());
    }

    [Theory]
    [InlineData("{\"assembly_name\":\"Demo\",\"files\":[{\"path\":\"../A.cs\",\"content\":\"\"}],\"packages\":[]}", "invalid_request")]
    [InlineData("{\"assembly_name\":\"Demo\",\"files\":[{\"path\":\"A.txt\",\"content\":\"\"}],\"packages\":[]}", "invalid_request")]
    [InlineData("{\"assembly_name\":\"Demo\",\"files\":[],\"packages\":[],\"project\":\"x.csproj\"}", "invalid_request")]
    [InlineData("{\"assembly_name\":\"Demo\",\"files\":[]}", "invalid_request")]
    [InlineData("not json", "invalid_request")]
    [InlineData("{\"assembly_name\":\"Demo\",\"files\":[],\"packages\":[{\"id\":\"Unknown\",\"version\":\"1.0.0\"}]}", "unknown_package")]
    public async Task BadRequestIs400(string body, string error)
    {
        await using var app = await Start(_sdk.Options());

        var response = await app.GetTestClient().PostAsync("/compile", new StringContent(body, Encoding.UTF8, "application/json"));
        using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal(error, json.RootElement.GetProperty("error").GetString());
    }

    [Fact]
    public async Task BodyOverTheLimitIs400()
    {
        await using var app = await Start(_sdk.Options(options => options.RequestMaxBytes = 1000));

        var response = await app.GetTestClient().PostAsJsonAsync("/compile", Body([("A.cs", new string('a', 2000))]));

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task TimeLimitCancelsTheCompilation()
    {
        await using var app = await Start(_sdk.Options(options => options.TimeoutSeconds = 1));
        var started = DateTime.UtcNow;

        var response = await app.GetTestClient().PostAsJsonAsync("/compile", Body([("Slow.cs", SlowSource)]));
        using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("timeout", json.RootElement.GetProperty("status").GetString());
        Assert.Equal(0, json.RootElement.GetProperty("diagnostics").GetArrayLength());
        Assert.Equal(JsonValueKind.Null, json.RootElement.GetProperty("rules").ValueKind);
        Assert.True(DateTime.UtcNow - started < TimeSpan.FromSeconds(20));

        // The service keeps working after a cancelled compilation.
        var next = await app.GetTestClient().PostAsJsonAsync("/compile", Body([("A.cs", "namespace Demo; public class A { }")]));
        Assert.Contains("\"succeeded\"", await next.Content.ReadAsStringAsync(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task CompilationOverTheParallelLimitIs429()
    {
        await using var app = await Start(_sdk.Options(options => (options.MaxParallel, options.TimeoutSeconds) = (1, 3)));
        var client = app.GetTestClient();

        var slow = client.PostAsJsonAsync("/compile", Body([("Slow.cs", SlowSource)]));
        HttpResponseMessage? busy = null;
        for (var attempt = 0; attempt < 50 && busy is null; attempt++)
        {
            await Task.Delay(50);
            var response = await client.PostAsJsonAsync("/compile", Body([("A.cs", "namespace Demo; public class A { }")]));
            if (response.StatusCode == HttpStatusCode.TooManyRequests)
            {
                busy = response;
            }
        }

        Assert.NotNull(busy);
        Assert.Equal("{\"error\":\"busy\"}", await busy!.Content.ReadAsStringAsync());
        Assert.Equal(HttpStatusCode.OK, (await slow).StatusCode);
    }

    private static object Body((string Path, string Content)[] files) => new
    {
        assembly_name = "Demo",
        files = files.Select(file => new { path = file.Path, content = file.Content }),
        packages = Array.Empty<object>(),
    };

    private static async Task<WebApplication> Start(SandboxOptions options)
    {
        var app = SandboxApp.Build([], builder =>
        {
            builder.WebHost.UseTestServer();
            builder.Services.Configure<SandboxOptions>(configured =>
            {
                configured.SdkDir = options.SdkDir;
                configured.IndexDir = options.IndexDir;
                configured.PackagesDir = options.PackagesDir;
                configured.ReferenceDir = options.ReferenceDir;
                configured.MaxParallel = options.MaxParallel;
                configured.TimeoutSeconds = options.TimeoutSeconds;
                configured.RequestMaxBytes = options.RequestMaxBytes;
            });
        });
        await app.StartAsync();
        return app;
    }
}
