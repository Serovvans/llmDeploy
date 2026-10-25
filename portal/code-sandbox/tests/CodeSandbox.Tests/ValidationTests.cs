namespace CodeSandbox.Tests;

public sealed class ValidationTests
{
    private static readonly SandboxOptions Options = new() { PackagesDir = TestSdk.NuGetPackages, MaxFiles = 3, FileMaxBytes = 100, ProjectMaxBytes = 150 };

    [Theory]
    [InlineData("../A.cs")]
    [InlineData("Tools/../../A.cs")]
    [InlineData("./A.cs")]
    [InlineData("/etc/A.cs")]
    [InlineData("C:/A.cs")]
    [InlineData("Tools\\A.cs")]
    [InlineData("A.txt")]
    [InlineData("A.CS")]
    [InlineData("Tools/A.csproj")]
    [InlineData("nul.cs")]
    [InlineData("Com1.Helper.cs")]
    [InlineData("aux/A.cs")]
    [InlineData("Tools./A.cs")]
    [InlineData("My Tool.cs")]
    [InlineData("Tools//A.cs")]
    [InlineData("")]
    [InlineData("a/b/c/d/e/f/g/h/i.cs")]
    public void BadPathIsRejected(string path)
    {
        var rejection = Validate([(path, "class A { }")]);

        Assert.Equal("invalid_request", rejection?.Error);
        Assert.DoesNotContain(path.Length > 0 ? path : "\0", rejection!.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("A.cs")]
    [InlineData("Tools/Address-Tool_2.Helper.cs")]
    [InlineData("a/b/c/d/e/f/g/h.cs")]
    public void GoodPathIsAccepted(string path) => Assert.Null(Validate([(path, "class A { }")]));

    [Fact]
    public void PathConflictsAreCaseInsensitive()
    {
        Assert.NotNull(Validate([("a.cs", ""), ("A.CS", "")]));
        Assert.NotNull(Validate([("a.cs", ""), ("A.CS/B.cs", "")]));
    }

    [Fact]
    public void LimitsAreChecked()
    {
        Assert.NotNull(Validate([("A.cs", ""), ("B.cs", ""), ("C.cs", ""), ("D.cs", "")]));
        Assert.NotNull(Validate([("A.cs", new string('я', 51))]));
        Assert.NotNull(Validate([("A.cs", new string('a', 100)), ("B.cs", new string('a', 51))]));
        Assert.NotNull(Validate([("A.cs", "class A { }\0")]));
        Assert.Null(Validate([("A.cs", new string('a', 100)), ("B.cs", new string('a', 50))]));
    }

    [Theory]
    [InlineData("class")]
    [InlineData("CON")]
    [InlineData("1Tool")]
    [InlineData("Address.Tool")]
    public void BadAssemblyNameIsRejected(string name)
    {
        var (_, rejection) = RequestValidator.Validate(new CompileRequest(name, [], []), Options);

        Assert.Equal("invalid_request", rejection?.Error);
    }

    [Theory]
    [InlineData("Npgsql", "1.0.0", "unknown_package")]
    [InlineData("NoSuchPackage", "9.0.3", "unknown_package")]
    [InlineData("../npgsql", "9.0.3", "invalid_request")]
    [InlineData("Npgsql", "../9.0.3", "invalid_request")]
    [InlineData("Npgsql", "9.0.3/..", "invalid_request")]
    public void PackageOutsideTheSetIsRejected(string id, string version, string error)
    {
        var (_, rejection) = RequestValidator.Validate(new CompileRequest("Demo", [], [new PackageRef(id, version)]), Options);

        Assert.Equal(error, rejection?.Error);
    }

    [Fact]
    public void PackageOfTheSetIsAccepted()
    {
        var (request, rejection) = RequestValidator.Validate(new CompileRequest("Demo", [], [new PackageRef("Npgsql", "9.0.3")]), Options);

        Assert.Null(rejection);
        Assert.EndsWith("npgsql/9.0.3/npgsql.9.0.3.nupkg", request!.Packages[0].NupkgPath, StringComparison.Ordinal);
    }

    private static Rejection? Validate((string Path, string Content)[] files) =>
        RequestValidator.Validate(
            new CompileRequest("Demo", files.Select(file => (SourceFile?)new SourceFile(file.Path, file.Content)).ToList(), []),
            Options).Rejection;
}
