namespace CodeSandbox.Tests;

public sealed class RulesTests : IDisposable
{
    private const string Factory = """
        using eLiteGIS.Geoprocessing;

        namespace Demo
        {
            public sealed class GpToolFactory : IGpToolFactory
            {
                public Type[] GetToolTypes()
                {
                    return new Type[] { typeof(FirstTool), typeof(SecondTool) };
                }
            }
        }
        """;

    private const string FirstTool = """
        using eLiteGIS.Geoprocessing;

        namespace Demo
        {
            [GpToolIcon("fa fa-cog")]
            [GpCategory("Утилиты")]
            [GpDescription("Первый", "First")]
            public sealed class FirstTool : GpTool
            {
                [GpRequired]
                [GpDescription("Вход", "Input")]
                public string? Input { get; set; }

                [GpDescription("Итог", "Result")]
                public string? Result { get; private set; }

                public override void InnerExecute()
                {
                    Result = Input;
                }
            }
        }
        """;

    private const string SecondTool = """
        using eLiteGIS.Geoprocessing;

        namespace Demo
        {
            [GpToolIcon("fa fa-map")]
            [GpCategory("Карты")]
            [GpDescription("Второй", englishDescription: "Second")]
            public sealed class SecondTool : GpTool
            {
                public override void InnerExecute()
                {
                }
            }
        }
        """;

    private readonly TestSdk _sdk = TestSdk.Create();

    public void Dispose() => _sdk.Dispose();

    [Fact]
    public void CorrectProjectHasNoFindings()
    {
        var rules = Check(Factory, FirstTool, SecondTool);

        Assert.Empty(rules.Findings);
        Assert.Equal("Demo.GpToolFactory", rules.Factory);
        Assert.Equal(
            [new ToolDto("Demo.FirstTool", "Первый", "Утилиты"), new ToolDto("Demo.SecondTool", "Второй", "Карты")],
            rules.Tools);
    }

    [Fact]
    public void CollectionExpressionAndExpressionBodyAreAccepted()
    {
        var factory = Factory.Replace(
            "public Type[] GetToolTypes()\n        {\n            return new Type[] { typeof(FirstTool), typeof(SecondTool) };\n        }",
            "public Type[] GetToolTypes() => [typeof(SecondTool), (typeof(FirstTool))];",
            StringComparison.Ordinal);
        Assert.Contains("=> [", factory, StringComparison.Ordinal);

        Assert.Empty(Check(factory, FirstTool, SecondTool).Findings);
    }

    [Fact]
    public void FactoryWithoutATool()
    {
        var factory = Factory.Replace(", typeof(SecondTool)", string.Empty, StringComparison.Ordinal);

        var finding = Assert.Single(Check(factory, FirstTool, SecondTool).Findings);

        Assert.Equal(("tool_types", "GpToolFactory.cs", TestSdk.LineOf(factory, "GetToolTypes")), (finding.Rule, finding.File, finding.Line));
        Assert.Contains("SecondTool", finding.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void ToolWithoutDescription()
    {
        var tool = SecondTool.Replace("[GpDescription(\"Второй\", englishDescription: \"Second\")]", "[GpDescription(\"Второй\")]", StringComparison.Ordinal);

        var rules = Check(Factory, FirstTool, tool);

        var finding = Assert.Single(rules.Findings);
        Assert.Equal(("tool_attributes", "SecondTool.cs", TestSdk.LineOf(tool, "class SecondTool")), (finding.Rule, finding.File, finding.Line));
        Assert.Equal("Второй", rules.Tools[1].Title);
    }

    [Theory]
    [InlineData("[GpToolIcon(\"fa fa-map\")]", "")]
    [InlineData("[GpCategory(\"Карты\")]", "[GpCategory(\" \")]")]
    [InlineData("[GpDescription(\"Второй\", englishDescription: \"Second\")]", "")]
    public void ToolWithoutAttribute(string attribute, string replacement)
    {
        var finding = Assert.Single(Check(Factory, FirstTool, SecondTool.Replace(attribute, replacement, StringComparison.Ordinal)).Findings);

        Assert.Equal(("tool_attributes", "SecondTool.cs"), (finding.Rule, finding.File));
    }

    [Theory]
    [InlineData("Result = Input;")]
    [InlineData("this.Result += Input;")]
    [InlineData("Action set = () => { Result ??= Input; }; set();")]
    [InlineData("void Set() => this.Result = Input; Set();")]
    public void OutputParameterWithPublicSetter(string body)
    {
        var tool = FirstTool
            .Replace("public string? Result { get; private set; }", "public string? Result { get; set; }", StringComparison.Ordinal)
            .Replace("Result = Input;", body, StringComparison.Ordinal);

        var finding = Assert.Single(Check(Factory, tool, SecondTool).Findings);

        Assert.Equal(("output_parameter", "FirstTool.cs", TestSdk.LineOf(tool, "public string? Result")), (finding.Rule, finding.File, finding.Line));
    }

    [Theory]
    [InlineData("{ get; init; }")]
    [InlineData("{ get; protected set; }")]
    [InlineData("{ get; internal set; }")]
    [InlineData("{ get; }")]
    [InlineData("{ private get; set; }")]
    public void ParameterAccessors(string accessors)
    {
        var tool = FirstTool.Replace("public string? Input { get; set; }", $"public string? Input {accessors}", StringComparison.Ordinal);

        var finding = Assert.Single(Check(Factory, tool, SecondTool).Findings);

        Assert.Equal(("parameter_accessors", "FirstTool.cs", TestSdk.LineOf(tool, "public string? Input")), (finding.Rule, finding.File, finding.Line));
    }

    [Fact]
    public void NoFactory()
    {
        var rules = Check(Factory.Replace(" : IGpToolFactory", string.Empty, StringComparison.Ordinal), FirstTool, SecondTool);

        var finding = Assert.Single(rules.Findings);
        Assert.Equal(("factory", null, null), (finding.Rule, finding.File, finding.Line));
        Assert.Null(rules.Factory);
    }

    [Fact]
    public void TwoFactories()
    {
        var second = Factory.Replace("class GpToolFactory", "class OtherFactory", StringComparison.Ordinal);

        var rules = Check(Factory, FirstTool, SecondTool, ("OtherFactory.cs", second));

        Assert.Equal(2, rules.Findings.Count(finding => finding.Rule == "factory" && finding.Line is not null));
        Assert.Null(rules.Factory);
    }

    [Fact]
    public void ToolTypesOfAnotherKind()
    {
        var factory = Factory.Replace(
            "return new Type[] { typeof(FirstTool), typeof(SecondTool) };",
            "var list = new List<Type> { typeof(FirstTool), typeof(SecondTool) }; return list.ToArray();",
            StringComparison.Ordinal);

        var finding = Assert.Single(Check(factory, FirstTool, SecondTool).Findings);

        Assert.Equal(("tool_types", TestSdk.LineOf(factory, "GetToolTypes")), (finding.Rule, finding.Line));
    }

    [Fact]
    public void ExtraAndRepeatedElements()
    {
        var factory = Factory.Replace("typeof(SecondTool) }", "typeof(SecondTool), typeof(string), typeof(FirstTool) }", StringComparison.Ordinal);

        var findings = Check(factory, FirstTool, SecondTool).Findings;

        Assert.Equal(2, findings.Count);
        Assert.All(findings, finding => Assert.Equal(("tool_types", TestSdk.LineOf(factory, "return")), (finding.Rule, finding.Line)));
        Assert.True(findings[0].Column < findings[1].Column);
    }

    [Fact]
    public void AbstractToolInTheList()
    {
        const string abstractTool = "namespace Demo { public abstract class BaseTool : eLiteGIS.Geoprocessing.GpTool { } }";
        var factory = Factory.Replace("typeof(SecondTool) }", "typeof(SecondTool), typeof(BaseTool) }", StringComparison.Ordinal);

        var finding = Assert.Single(Check(factory, FirstTool, SecondTool, ("BaseTool.cs", abstractTool)).Findings);

        Assert.Contains("абстрактный", finding.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void NoTools()
    {
        var factory = Factory.Replace("typeof(FirstTool), typeof(SecondTool)", string.Empty, StringComparison.Ordinal);

        var finding = Assert.Single(Check(factory).Findings);

        Assert.Equal(("tool_types", null), (finding.Rule, finding.File));
    }

    [Fact]
    public void GlobalNamespace()
    {
        const string tool = """
            using eLiteGIS.Geoprocessing;

            [GpToolIcon("fa fa-map")]
            [GpCategory("Карты")]
            [GpDescription("Второй", "Second")]
            public sealed class SecondTool : GpTool
            {
                public override void InnerExecute()
                {
                }
            }
            """;

        var finding = Assert.Single(Check(Factory, FirstTool, tool).Findings);

        Assert.Equal(("namespace", "SecondTool.cs", TestSdk.LineOf(tool, "class SecondTool")), (finding.Rule, finding.File, finding.Line));
    }

    private RulesDto Check(string factory, params object[] files)
    {
        var all = new List<(string, string)> { ("GpToolFactory.cs", factory) };
        foreach (var file in files)
        {
            all.Add(file switch
            {
                (string path, string content) => (path, content),
                string content when content.Contains("class FirstTool", StringComparison.Ordinal) => ("FirstTool.cs", content),
                string content => ("SecondTool.cs", content),
                _ => throw new ArgumentException("file"),
            });
        }

        var response = _sdk.Compile(all);
        Assert.True(response.Status == "succeeded", string.Join("\n", response.Diagnostics.Select(diagnostic => $"{diagnostic.File}:{diagnostic.Line} {diagnostic.Code} {diagnostic.Message}")));
        return response.Rules!;
    }
}
