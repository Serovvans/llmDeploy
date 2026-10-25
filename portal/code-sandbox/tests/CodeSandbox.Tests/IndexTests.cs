using System.Text.Json;

namespace CodeSandbox.Tests;

public sealed class IndexTests
{
    [Fact]
    public void ReferenceIsBuiltFromTheStubSdk()
    {
        using var sdk = TestSdk.Create(plugins: "  Npgsql.dll  \n\nreadme.txt\nNewtonsoft.Json.dll\n");

        var manifest = Read<SdkManifest>(sdk, SdkManifest.FileName);
        var reference = Read<ApiReference>(sdk, ApiReference.FileName);

        Assert.Equal("stub", manifest.SdkVersion);
        Assert.Equal(["Atemiko.eLiteMap.dll", "eLiteGIS.Server.dll"], manifest.Files.Select(file => file.FileName));
        Assert.All(manifest.Files, file => Assert.Equal((SdkKinds.Cogis, 64), (file.Kind, file.Sha256.Length)));
        Assert.Equal(["Npgsql.dll", "Newtonsoft.Json.dll"], manifest.Plugins);

        Assert.Equal("stub", reference.SdkVersion);
        var gpTool = reference.Types.Single(type => type is { Namespace: "eLiteGIS.Geoprocessing", Name: "GpTool" });
        Assert.Equal(("class", "cogis", "public abstract class GpTool"), (gpTool.Kind, gpTool.Origin, gpTool.Signature));
        Assert.Contains(new ApiMember("method", "ValidateInput", "public virtual void ValidateInput()"), gpTool.Members);
        var description = reference.Types.Single(type => type.Name == "GpDescriptionAttribute");
        Assert.Contains(description.Members, member => member.Signature == "public GpDescriptionAttribute(string description, string? englishDescription = null)");
        var names = reference.Types.Single(type => type is { Namespace: "Atemiko.eLiteMap.Geodatabase", Name: "WorkspaceName.PropertyNames" });
        Assert.Contains(new ApiMember("field", "ConnectionString", "public const string ConnectionString = \"ConnectionString\""), names.Members);
        Assert.Contains(reference.Types, type => type is { Namespace: "eLiteGIS.Utilities", Name: "UserException", Origin: "cogis" });
        Assert.Contains(reference.Types, type => type is { Namespace: "System", Name: "String", Origin: "dotnet" });
        Assert.DoesNotContain(reference.Types, type => type.Namespace == "System.Runtime.CompilerServices");
        Assert.Equal(reference.Types.OrderBy(type => type.Namespace, StringComparer.Ordinal).ThenBy(type => type.Name, StringComparer.Ordinal).Select(type => type.Name), reference.Types.Select(type => type.Name));
    }

    [Fact]
    public void ServerLibrariesAndFrameworkCopiesGetTheirKind()
    {
        var extra = new Dictionary<string, byte[]>
        {
            ["Npgsql.dll"] = TestSdk.BuildLibrary("Npgsql", "namespace Npgsql { public class C { } }"),
            ["System.Text.Json.dll"] = TestSdk.BuildLibrary("System.Text.Json", "namespace System.Text.Json { public class C { } }"),
            ["libnative.dll"] = [0x4D, 0x5A, 1, 2, 3],
        };
        using var sdk = TestSdk.Create(extraFiles: extra);

        var kinds = Read<SdkManifest>(sdk, SdkManifest.FileName).Files.ToDictionary(file => file.FileName, file => file.Kind);

        Assert.Equal(SdkKinds.Server, kinds["Npgsql.dll"]);
        Assert.Equal(SdkKinds.Framework, kinds["System.Text.Json.dll"]);
        Assert.False(kinds.ContainsKey("libnative.dll"));
    }

    [Theory]
    [InlineData("plugins.txt")]
    [InlineData("Atemiko.eLiteMap.dll")]
    public void MissingRequiredFileFailsWithoutWriting(string missing)
    {
        using var sdk = TestSdk.Create(index: false);
        File.Delete(Path.Combine(sdk.SdkDir, missing));
        var error = new StringWriter();

        Assert.Equal(1, SdkIndexer.Run(sdk.SdkDir, sdk.IndexDir, TestSdk.ReferenceDir, TextWriter.Null, error));
        Assert.Contains(missing, error.ToString(), StringComparison.Ordinal);
        Assert.False(Directory.Exists(sdk.IndexDir));
    }

    [Fact]
    public void RequiredFileThatIsNotAnAssemblyFails()
    {
        using var sdk = TestSdk.Create(index: false);
        File.WriteAllBytes(Path.Combine(sdk.SdkDir, "eLiteGIS.Server.dll"), [1, 2, 3]);

        Assert.Equal(1, SdkIndexer.Run(sdk.SdkDir, sdk.IndexDir, TestSdk.ReferenceDir, TextWriter.Null, TextWriter.Null));
        Assert.False(Directory.Exists(sdk.IndexDir));
    }

    [Fact]
    public void ChangedSdkFileUnloadsTheSdk()
    {
        using var sdk = TestSdk.Create();
        File.AppendAllText(Path.Combine(sdk.SdkDir, "Atemiko.eLiteMap.dll"), " ");

        var (state, reason) = SdkState.Load(sdk.IndexDir, sdk.SdkDir);

        Assert.Equal(("hash_mismatch", null), (reason, state.Version));
    }

    private static T Read<T>(TestSdk sdk, string name) =>
        JsonSerializer.Deserialize<T>(File.ReadAllBytes(Path.Combine(sdk.IndexDir, name)), Json.Options)!;
}
