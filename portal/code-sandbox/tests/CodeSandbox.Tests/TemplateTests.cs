namespace CodeSandbox.Tests;

/// <summary>Wizard skeleton and example build with the stub SDK and pass the GPTool rules (ПД §10, п. 33).</summary>
public sealed class TemplateTests : IDisposable
{
    private static readonly (string, string)[] DatabasePackages =
    [
        ("Npgsql", "9.0.3"),
        ("Microsoft.Extensions.Logging.Abstractions", "8.0.2"),
        ("Microsoft.Extensions.DependencyInjection.Abstractions", "8.0.2"),
    ];

    private static readonly WizardParameter[] AllInputs =
    [
        new("Email", "string", true, "Email-адрес", "Email Address"),
        new("Comment", "string", false, "Комментарий «с кавычками» \"и\" \\", "Comment"),
        new("Count", "int", true, "Количество", "Count"),
        new("Ratio", "double", false, "Доля", "Ratio"),
        new("Enabled", "bool", false, "Включено", "Enabled"),
        new("Since", "datetime", true, "С даты", "Since"),
    ];

    private static readonly WizardParameter[] Outputs = [new("Result", "string", false, "Результат выполнения", "Execution result")];

    private readonly TestSdk _sdk = TestSdk.Create();

    public static TheoryData<string> Forms => new() { "one", "one-database", "several", "several-database", "empty" };

    public void Dispose() => _sdk.Dispose();

    [Theory]
    [MemberData(nameof(Forms))]
    public void SkeletonBuildsAndPassesRules(string form)
    {
        WizardTool Tool(string className, bool database, WizardParameter[] inputs, WizardParameter[] outputs) =>
            new(className, $"Инструмент {className}", $"{className} tool", "Утилиты", "fa fa-cog", database, inputs, outputs);
        WizardTool[] tools = form switch
        {
            "one" => [Tool("AddContactTool", false, AllInputs, Outputs)],
            "one-database" => [Tool("AddContactTool", true, AllInputs, Outputs)],
            "several" => [Tool("FirstTool", false, AllInputs, Outputs), Tool("SecondTool", false, [], []), Tool("Tool3", false, [], Outputs)],
            "several-database" => [Tool("FirstTool", true, AllInputs, Outputs), Tool("SecondTool", false, AllInputs[..1], []), Tool("ThirdTool", true, [], [])],
            _ => [Tool("Tool1", false, [], [])],
        };

        var (files, usesDatabase) = WizardTemplates.Render("AddressTool", tools);
        var response = _sdk.Compile(files, usesDatabase ? DatabasePackages : []);

        Assert.True(response.Status == "succeeded", Describe(response));
        Assert.Empty(response.Diagnostics);
        Assert.Empty(response.Rules!.Findings);
        Assert.Equal("AddressTool.GpToolFactory", response.Rules.Factory);
        Assert.Equal(tools.Select(tool => $"AddressTool.{tool.ClassName}"), response.Rules.Tools.Select(tool => tool.Name));
    }

    [Fact]
    public void ExampleBuildsAndPassesRules()
    {
        var response = _sdk.Compile(WizardTemplates.Example("ExampleTool"), DatabasePackages);

        Assert.True(response.Status == "succeeded", Describe(response));
        Assert.Empty(response.Diagnostics);
        Assert.Empty(response.Rules!.Findings);
        Assert.Equal("ExampleTool.GpToolFactory", response.Rules.Factory);
        Assert.Single(response.Rules.Tools);
    }

    [Fact]
    public void SkeletonBuildsWithFrameworkCopyInTheSdkFolder()
    {
        // A server copy of a .NET 8 assembly gets the "framework" kind and is not referenced (no CS1703 or CS0433).
        var copy = TestSdk.BuildLibrary("System.Text.Json", "namespace System.Text.Json { public static class JsonSerializer { } }");
        using var sdk = TestSdk.Create(extraFiles: new Dictionary<string, byte[]> { ["System.Text.Json.dll"] = copy });
        var (files, _) = WizardTemplates.Render("AddressTool", [new("FirstTool", "Первый", "First", "Утилиты", "fa fa-cog", false, AllInputs, Outputs)]);
        files.Add(("Json.cs", "namespace AddressTool; internal static class Json { public static string Write() => System.Text.Json.JsonSerializer.Serialize(1); }"));

        var response = sdk.Compile(files);

        Assert.True(response.Status == "succeeded", Describe(response));
        Assert.Empty(response.Rules!.Findings);
    }

    private static string Describe(CompileResponse response) =>
        string.Join("\n", response.Diagnostics.Select(diagnostic => $"{diagnostic.File}:{diagnostic.Line} {diagnostic.Code} {diagnostic.Message}"));
}
