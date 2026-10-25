using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeSandbox.Tests;

public sealed class CompileTests : IDisposable
{
    private readonly TestSdk _sdk = TestSdk.Create();

    public void Dispose() => _sdk.Dispose();

    [Fact]
    public void SucceedsWithDllAndSdkVersion()
    {
        var response = _sdk.Compile([("Lib/Calc.cs", "namespace Demo.Lib; public static class Calc { public static int Add(int a, int b) => a + b; }")]);

        Assert.Equal("succeeded", response.Status);
        Assert.Equal("stub", response.SdkVersion);
        Assert.Empty(response.Diagnostics);
        Assert.NotNull(response.Rules);
        Assert.NotNull(response.Dll);
        Assert.Equal((byte)'M', response.Dll![0]);
    }

    [Fact]
    public void ErrorIsInRussianWithFileAndLine()
    {
        const string source = "namespace Demo;\npublic class A\n{\n    public int F() => connStr;\n}\n";
        var response = _sdk.Compile([("Tools/A.cs", source)]);

        Assert.Equal("failed", response.Status);
        Assert.Null(response.Rules);
        Assert.Null(response.Dll);
        var error = Assert.Single(response.Diagnostics);
        Assert.Equal(("error", "CS0103", "Tools/A.cs", 4, 23, 4, 30), (error.Severity, error.Code, error.File, error.Line, error.Column, error.EndLine, error.EndColumn));
        Assert.Contains("не существует в текущем контексте", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void WarningDoesNotStopTheBuild()
    {
        var response = _sdk.Compile([("A.cs", "namespace Demo; public class A { public void F() { int unused; } }")]);

        Assert.Equal("succeeded", response.Status);
        var warning = Assert.Single(response.Diagnostics);
        Assert.Equal(("warning", "CS0168"), (warning.Severity, warning.Code));
        Assert.NotNull(response.Dll);
    }

    [Fact]
    public void ErrorsComeFirstAndAreTruncatedAtTheLimit()
    {
        const string source = "namespace Demo; public class A { public void F() { int w; var a = x1; var b = x2; var c = x3; } }";
        var response = _sdk.Compile([("A.cs", source)], configure: options => options.DiagnosticsMax = 2);

        Assert.True(response.DiagnosticsTruncated);
        Assert.Equal(2, response.Diagnostics.Count);
        Assert.All(response.Diagnostics, diagnostic => Assert.Equal("error", diagnostic.Severity));
    }

    [Fact]
    public void ImplicitUsingsOfTheClassLibraryTemplateAreThere()
    {
        var response = _sdk.Compile([("A.cs", "namespace Demo; public class A { public List<string> F() => File.ReadLines(\"x\").ToList(); }")]);

        Assert.Equal("succeeded", response.Status);
    }

    [Fact]
    public void OptionsHaveNoResolversAndNoStrongNameProvider()
    {
        var options = Compiler.CreateCompilationOptions();

        Assert.Null(options.XmlReferenceResolver);
        Assert.Null(options.SourceReferenceResolver);
        Assert.Null(options.MetadataReferenceResolver);
        Assert.Null(options.StrongNameProvider);
        Assert.Null(options.CryptoKeyFile);
        Assert.Null(options.CryptoKeyContainer);
        Assert.Null(options.DelaySign);
        Assert.False(options.PublicSign);
        Assert.False(options.AllowUnsafe);
        Assert.Equal(SourceCodeKind.Regular, Compiler.ParseOptions.Kind);
        Assert.Equal(LanguageVersion.CSharp12, Compiler.ParseOptions.LanguageVersion);
    }

    [Theory]
    [InlineData("[assembly: System.Reflection.AssemblyKeyFile(\"{0}\")]\nnamespace Demo {{ public class A {{ }} }}", true)]
    [InlineData("[assembly: System.Reflection.AssemblyKeyName(\"k\")]\nnamespace Demo {{ public class A {{ }} }}", true)]
    [InlineData("namespace Demo {{\n/// <include file=\"{0}\" path=\"doc/*\"/>\npublic class A {{ }} }}", false)]
    [InlineData("#r \"{0}\"\nnamespace Demo {{ public class A {{ }} }}", true)]
    [InlineData("#load \"{0}\"\nnamespace Demo {{ public class A {{ }} }}", true)]
    public void CompilerReadsNoFiles(string template, bool fails)
    {
        const string secret = "MARKER-7f3a9c-SECRET";
        var marker = Path.Combine(_sdk.Root, "marker.txt");
        File.WriteAllText(marker, $"<doc><summary>{secret}</summary></doc>\nnamespace Demo {{ public class {secret.Replace('-', '_')} {{ }} }}");

        var response = _sdk.Compile([("A.cs", string.Format(template, marker))]);

        Assert.Equal(fails ? "failed" : "succeeded", response.Status);
        if (fails)
        {
            Assert.Contains(response.Diagnostics, diagnostic => diagnostic.Severity == "error");
        }

        Assert.DoesNotContain(secret, TestSdk.Serialize(response), StringComparison.Ordinal);
        Assert.DoesNotContain(secret.Replace('-', '_'), TestSdk.Serialize(response), StringComparison.Ordinal);
    }

    [Fact]
    public void GeneratorAndAnalyzerOfAPackageAreNotExecuted()
    {
        var packages = Path.Combine(_sdk.Root, "packages");
        var ranMarker = Path.Combine(_sdk.Root, "generator-ran.txt");
        string[] roslyn =
        [
            typeof(IIncrementalGenerator).Assembly.Location,
            typeof(System.Collections.Immutable.ImmutableArray).Assembly.Location,
            typeof(System.Reflection.Metadata.MetadataReader).Assembly.Location,
        ];
        var generator = TestSdk.BuildLibrary("Evil.Generators", $$"""
            using Microsoft.CodeAnalysis;
            using Microsoft.CodeAnalysis.Diagnostics;
            using System.Collections.Immutable;
            [Generator]
            public sealed class EvilGenerator : IIncrementalGenerator
            {
                public void Initialize(IncrementalGeneratorInitializationContext context)
                {
                    System.IO.File.WriteAllText(@"{{ranMarker}}", "generator");
                    context.RegisterPostInitializationOutput(output => output.AddSource("G.cs", "namespace Evil { public static class Generated { } }"));
                }
            }
            [DiagnosticAnalyzer(LanguageNames.CSharp)]
            public sealed class EvilAnalyzer : DiagnosticAnalyzer
            {
                public override ImmutableArray<DiagnosticDescriptor> SupportedDiagnostics => ImmutableArray<DiagnosticDescriptor>.Empty;
                public override void Initialize(AnalysisContext context) => System.IO.File.WriteAllText(@"{{ranMarker}}", "analyzer");
            }
            """, roslyn);
        var library = TestSdk.BuildLibrary("Evil.Lib", "namespace Evil { public static class Helper { public static int One() => 1; } }");
        TestSdk.WritePackage(packages, "Evil.Lib", "1.0.0", new Dictionary<string, byte[]>
        {
            ["lib/net8.0/Evil.Lib.dll"] = library,
            ["analyzers/dotnet/cs/Evil.Generators.dll"] = generator,
            ["build/Evil.Lib.targets"] = "<Project><Target Name=\"Evil\" BeforeTargets=\"Build\" /></Project>"u8.ToArray(),
            ["tools/net8.0/Evil.Generators.dll"] = generator,
        });
        using var sdk = TestSdk.Create(packagesDir: packages);

        var usesGenerated = sdk.Compile([("A.cs", "namespace Demo; public class A { public int F() => Evil.Helper.One(); public object G() => typeof(Evil.Generated); }")], [("Evil.Lib", "1.0.0")]);
        var usesLibrary = sdk.Compile([("A.cs", "namespace Demo; public class A { public int F() => Evil.Helper.One(); }")], [("Evil.Lib", "1.0.0")]);

        Assert.Equal("failed", usesGenerated.Status);
        Assert.Contains(usesGenerated.Diagnostics, diagnostic => diagnostic.Code == "CS0234");
        Assert.Equal("succeeded", usesLibrary.Status);
        Assert.Equal(["Evil.Lib.dll"], usesLibrary.Bundle.Select(file => file.FileName));
        Assert.Equal(library, usesLibrary.Bundle[0].Content);
        Assert.False(File.Exists(ranMarker));
    }

    [Fact]
    public void ServerLibraryHasPriorityOverThePackage()
    {
        var packages = Path.Combine(_sdk.Root, "packages");
        TestSdk.WritePackage(packages, "Foo.Lib", "1.0.0", new Dictionary<string, byte[]>
        {
            ["lib/netstandard2.0/Foo.Lib.dll"] = TestSdk.BuildLibrary("Foo.Lib", "namespace Foo { public static class PackageOnly { } }"),
        });
        var server = new Dictionary<string, byte[]>
        {
            ["Foo.Lib.dll"] = TestSdk.BuildLibrary("Foo.Lib", "namespace Foo { public static class ServerOnly { } }"),
        };
        using var sdk = TestSdk.Create(extraFiles: server, packagesDir: packages);

        var server1 = sdk.Compile([("A.cs", "namespace Demo; public class A { public object F() => typeof(Foo.ServerOnly); }")], [("Foo.Lib", "1.0.0")]);
        var package1 = sdk.Compile([("A.cs", "namespace Demo; public class A { public object F() => typeof(Foo.PackageOnly); }")], [("Foo.Lib", "1.0.0")]);

        Assert.Equal("succeeded", server1.Status);
        Assert.Empty(server1.Bundle);
        Assert.Equal(["Foo.Lib.dll"], server1.OnServer);
        Assert.Equal("failed", package1.Status);
    }

    [Fact]
    public void PackagesOnServerByPluginsAreNotBundled()
    {
        // Stub plugins.txt lists Npgsql.dll only: its dependencies go to the archive.
        var response = _sdk.Compile(
            [("A.cs", "using Npgsql; namespace Demo; public class A { public object F() => new NpgsqlConnectionStringBuilder { Host = \"h\" }; }")],
            [("Npgsql", "9.0.3"), ("Microsoft.Extensions.Logging.Abstractions", "8.0.2"), ("Microsoft.Extensions.DependencyInjection.Abstractions", "8.0.2")]);

        Assert.Equal("succeeded", response.Status);
        Assert.Equal(["Npgsql.dll"], response.OnServer);
        Assert.Equal(
            ["Microsoft.Extensions.Logging.Abstractions.dll", "Microsoft.Extensions.DependencyInjection.Abstractions.dll"],
            response.Bundle.Select(file => file.FileName));
    }

    [Fact]
    public void CancellationGivesOperationCanceled()
    {
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();

        Assert.ThrowsAny<OperationCanceledException>(() => _sdk.Compile([("A.cs", "namespace Demo; public class A { }")], cancellationToken: cancelled.Token));
    }
}
