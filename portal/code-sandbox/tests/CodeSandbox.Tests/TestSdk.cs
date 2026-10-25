using System.IO.Compression;
using System.Reflection;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeSandbox.Tests;

/// <summary>Temporary SDK folder with the stub DLLs, its index and an optional package folder.</summary>
internal sealed class TestSdk : IDisposable
{
    private static readonly Lazy<ReferencePack> SharedPack = new(() => ReferencePack.Load(ReferenceDir));

    private TestSdk(string root, string packagesDir)
    {
        Root = root;
        PackagesDir = packagesDir;
    }

    /// <summary>.NET 8 reference assemblies next to the tests.</summary>
    public static string ReferenceDir => Path.Combine(AppContext.BaseDirectory, "ref");

    /// <summary>Reference pack shared by all tests.</summary>
    public static ReferencePack Pack => SharedPack.Value;

    /// <summary>NuGet folder of the test build: the offline set in the nuget.org layout.</summary>
    public static string NuGetPackages => Metadata("NuGetPackageRoot");

    /// <summary>Wizard templates of the backend configuration.</summary>
    public static string TemplatesDir => Metadata("TemplatesDir");

    public string Root { get; }

    public string SdkDir => Path.Combine(Root, "sdk");

    public string IndexDir => Path.Combine(Root, "index");

    public string PackagesDir { get; }

    /// <summary>Creates the SDK folder (stub DLLs, <c>plugins.txt</c>, extra files) and runs the index.</summary>
    public static TestSdk Create(string plugins = "Npgsql.dll\n", IDictionary<string, byte[]>? extraFiles = null, string? packagesDir = null, bool index = true)
    {
        var root = Directory.CreateTempSubdirectory("code-sandbox-test-").FullName;
        var sdk = new TestSdk(root, packagesDir ?? NuGetPackages);
        Directory.CreateDirectory(sdk.SdkDir);
        foreach (var name in SdkIndexer.RequiredFiles)
        {
            File.Copy(Path.Combine(AppContext.BaseDirectory, name), Path.Combine(sdk.SdkDir, name));
        }

        File.WriteAllText(Path.Combine(sdk.SdkDir, SdkIndexer.PluginsFile), plugins);
        foreach (var (name, content) in extraFiles ?? new Dictionary<string, byte[]>())
        {
            File.WriteAllBytes(Path.Combine(sdk.SdkDir, name), content);
        }

        if (index && SdkIndexer.Run(sdk.SdkDir, sdk.IndexDir, ReferenceDir, TextWriter.Null, TextWriter.Null) != 0)
        {
            throw new InvalidOperationException("Index of the test SDK failed.");
        }

        return sdk;
    }

    public SandboxOptions Options(Action<SandboxOptions>? configure = null)
    {
        var options = new SandboxOptions
        {
            SdkDir = SdkDir,
            IndexDir = IndexDir,
            PackagesDir = PackagesDir,
            ReferenceDir = ReferenceDir,
        };
        configure?.Invoke(options);
        return options;
    }

    /// <summary>Validates and compiles like <c>POST /compile</c>, without HTTP.</summary>
    public CompileResponse Compile(IEnumerable<(string Path, string Content)> files, IEnumerable<(string Id, string Version)>? packages = null, Action<SandboxOptions>? configure = null, CancellationToken cancellationToken = default)
    {
        var options = Options(configure);
        var request = new CompileRequest(
            "Demo",
            files.Select(file => (SourceFile?)new SourceFile(file.Path, file.Content)).ToList(),
            (packages ?? []).Select(package => (PackageRef?)new PackageRef(package.Id, package.Version)).ToList());
        var (valid, rejection) = RequestValidator.Validate(request, options);
        Assert.Null(rejection);
        var (state, reason) = SdkState.Load(options.IndexDir, options.SdkDir);
        Assert.Equal("ok", reason);
        return new Compiler(Pack, state, options).Compile(valid!, cancellationToken).Response;
    }

    public void Dispose() => Directory.Delete(Root, recursive: true);

    /// <summary>Compiles a helper library for a test SDK folder or package; never loaded.</summary>
    public static byte[] BuildLibrary(string assemblyName, string source, IReadOnlyList<string>? referencePaths = null)
    {
        // A library from the test folder replaces the reference assembly of the same name (newer version).
        var extra = (referencePaths ?? []).ToDictionary(path => Path.GetFileName(path), path => (MetadataReference)MetadataReference.CreateFromFile(path));
        var compilation = CSharpCompilation.Create(
            assemblyName,
            [CSharpSyntaxTree.ParseText(source)],
            Pack.References.Where(reference => !extra.ContainsKey(reference.Display!)).Concat(extra.Values),
            new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));
        using var stream = new MemoryStream();
        var result = compilation.Emit(stream);
        Assert.True(result.Success, string.Join("\n", result.Diagnostics));
        return stream.ToArray();
    }

    /// <summary>Writes a <c>.nupkg</c> in the nuget.org layout of <paramref name="packagesDir"/>.</summary>
    public static void WritePackage(string packagesDir, string id, string version, IDictionary<string, byte[]> entries)
    {
        var folder = Path.Combine(packagesDir, id.ToLowerInvariant(), version);
        Directory.CreateDirectory(folder);
        using var archive = ZipFile.Open(Path.Combine(folder, $"{id.ToLowerInvariant()}.{version}.nupkg"), ZipArchiveMode.Create);
        foreach (var (name, content) in entries)
        {
            using var stream = archive.CreateEntry(name).Open();
            stream.Write(content);
        }
    }

    /// <summary>1-based line of the first occurrence of <paramref name="text"/>.</summary>
    public static int LineOf(string source, string text)
    {
        var index = source.IndexOf(text, StringComparison.Ordinal);
        Assert.True(index >= 0, $"'{text}' not found");
        return source[..index].Count(c => c == '\n') + 1;
    }

    public static string Serialize(CompileResponse response) => JsonSerializer.Serialize(response, Json.Options);

    private static string Metadata(string key) =>
        typeof(TestSdk).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>().Single(attribute => attribute.Key == key).Value!;
}
