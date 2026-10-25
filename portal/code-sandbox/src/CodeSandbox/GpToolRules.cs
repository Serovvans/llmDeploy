using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace CodeSandbox;

/// <summary>
/// GPTool rules of portal-api.md §7.5.13 on the semantic model of a successful compilation;
/// nothing is executed. Types are matched by full metadata names from the configuration.
/// </summary>
internal sealed class GpToolRules(GpToolNames names)
{
    private const string GetToolTypesName = "GetToolTypes";
    private const string InnerExecuteName = "InnerExecute";

    /// <summary>Checks the project files of the compilation.</summary>
    public RulesDto Check(CSharpCompilation compilation, IReadOnlyList<SyntaxTree> projectTrees, CancellationToken cancellationToken)
    {
        var classes = ProjectClasses(compilation, projectTrees, cancellationToken);
        var tools = classes.Where(type => !type.IsAbstract && InheritsFrom(type, names.ToolBase)).ToList();
        var factories = classes
            .Where(type => !type.IsAbstract && IsEffectivelyPublic(type)
                && type.AllInterfaces.Any(iface => FullName(iface) == names.Factory))
            .ToList();
        var findings = new List<FindingDto>();

        string? factoryName = null;
        if (factories.Count == 0)
        {
            findings.Add(Finding("factory", null, "В проекте нет открытого класса, реализующего IGpToolFactory: CoGIS не найдёт инструменты."));
        }
        else if (factories.Count > 1)
        {
            findings.AddRange(factories.Select(factory => Finding(
                "factory", factory.Locations[0], $"Класс «{factory.Name}» — одна из нескольких фабрик IGpToolFactory; в проекте должна быть одна.")));
        }
        else
        {
            factoryName = ReflectionName(factories[0]);
            CheckToolTypes(compilation, factories[0], tools, findings, cancellationToken);
        }

        if (tools.Count == 0)
        {
            findings.Add(Finding("tool_types", null, "В проекте нет инструментов — неабстрактных классов, наследующих GpTool."));
        }

        var toolDtos = new List<ToolDto>(tools.Count);
        foreach (var tool in tools)
        {
            toolDtos.Add(CheckAttributes(tool, findings));
            CheckParameters(compilation, tool, findings, cancellationToken);
        }

        foreach (var type in factories.Concat(tools).Distinct<INamedTypeSymbol>(SymbolEqualityComparer.Default))
        {
            if (type.ContainingNamespace.IsGlobalNamespace)
            {
                findings.Add(Finding("namespace", type.Locations[0], $"Класс «{type.Name}» объявлен вне пространства имён: задайте namespace."));
            }
        }

        var ordered = findings
            .OrderBy(finding => finding.File is null ? 0 : 1)
            .ThenBy(finding => finding.File, StringComparer.OrdinalIgnoreCase)
            .ThenBy(finding => finding.Line)
            .ThenBy(finding => finding.Column)
            .ThenBy(finding => finding.Rule, StringComparer.Ordinal)
            .ToList();
        return new RulesDto(factoryName, toolDtos, ordered);
    }

    private void CheckToolTypes(
        CSharpCompilation compilation, INamedTypeSymbol factory, List<INamedTypeSymbol> tools, List<FindingDto> findings, CancellationToken cancellationToken)
    {
        var method = FindGetToolTypes(factory);
        var syntax = method?.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax(cancellationToken) as MethodDeclarationSyntax;
        if (method is null || syntax is null)
        {
            findings.Add(Finding("tool_types", factory.Locations[0], $"У фабрики «{factory.Name}» нет метода GetToolTypes в коде проекта."));
            return;
        }

        var methodLocation = syntax.Identifier.GetLocation();
        var elements = ArrayElements(syntax);
        if (elements is null)
        {
            findings.Add(Finding("tool_types", methodLocation, "Тело GetToolTypes — один return с массивом из typeof(…) инструментов: иначе список не проверить без запуска кода."));
            return;
        }

        var model = compilation.GetSemanticModel(syntax.SyntaxTree);
        var toolSet = new HashSet<INamedTypeSymbol>(tools, SymbolEqualityComparer.Default);
        var listed = new HashSet<INamedTypeSymbol>(SymbolEqualityComparer.Default);
        foreach (var element in elements)
        {
            var type = (model.GetTypeInfo(element.Type, cancellationToken).Type as INamedTypeSymbol)?.OriginalDefinition;
            var display = type?.Name ?? element.Type.ToString();
            if (type is null || !toolSet.Contains(type))
            {
                var reason = type is not null && type.IsAbstract && InheritsFrom(type, names.ToolBase)
                    ? $"«{display}» — абстрактный класс: CoGIS не создаст такой инструмент."
                    : $"«{display}» — не инструмент проекта (неабстрактный класс, наследующий GpTool).";
                findings.Add(Finding("tool_types", element.GetLocation(), reason));
            }
            else if (!listed.Add(type))
            {
                findings.Add(Finding("tool_types", element.GetLocation(), $"«{display}» уже есть в списке GetToolTypes."));
            }
        }

        foreach (var tool in tools.Where(tool => !listed.Contains(tool)))
        {
            findings.Add(Finding("tool_types", methodLocation, $"Инструмент «{tool.Name}» не указан в GetToolTypes: CoGIS его не покажет."));
        }
    }

    private IMethodSymbol? FindGetToolTypes(INamedTypeSymbol factory)
    {
        var declared = factory.AllInterfaces
            .Where(iface => FullName(iface) == names.Factory)
            .SelectMany(iface => iface.GetMembers(GetToolTypesName).OfType<IMethodSymbol>())
            .FirstOrDefault(method => method.Parameters.Length == 0);
        if (declared is not null)
        {
            return factory.FindImplementationForInterfaceMember(declared) as IMethodSymbol;
        }

        for (var type = factory; type is not null; type = type.BaseType)
        {
            var method = type.GetMembers(GetToolTypesName).OfType<IMethodSymbol>()
                .FirstOrDefault(candidate => candidate.Parameters.Length == 0 && !candidate.IsStatic);
            if (method is not null)
            {
                return method;
            }
        }

        return null;
    }

    /// <summary>
    /// <c>typeof</c> elements of the single returned array — <c>new Type[] { … }</c>,
    /// <c>new[] { … }</c> or <c>[ … ]</c>; <c>null</c> — the body is of another kind.
    /// </summary>
    private static List<TypeOfExpressionSyntax>? ArrayElements(MethodDeclarationSyntax method)
    {
        ExpressionSyntax? returned = method switch
        {
            { ExpressionBody: { } arrow } => arrow.Expression,
            { Body.Statements: [ReturnStatementSyntax { Expression: { } expression }] } => expression,
            _ => null,
        };

        IEnumerable<ExpressionSyntax?>? items = Unwrap(returned) switch
        {
            ArrayCreationExpressionSyntax { Initializer: { } initializer } => initializer.Expressions,
            ImplicitArrayCreationExpressionSyntax creation => creation.Initializer.Expressions,
            CollectionExpressionSyntax collection => collection.Elements
                .Select(element => element is ExpressionElementSyntax item ? item.Expression : null),
            _ => null,
        };

        if (items is null)
        {
            return null;
        }

        var result = new List<TypeOfExpressionSyntax>();
        foreach (var item in items)
        {
            if (Unwrap(item) is not TypeOfExpressionSyntax typeOf)
            {
                return null;
            }

            result.Add(typeOf);
        }

        return result;
    }

    private ToolDto CheckAttributes(INamedTypeSymbol tool, List<FindingDto> findings)
    {
        var location = tool.Locations[0];
        var attributes = tool.GetAttributes();

        var description = attributes.FirstOrDefault(attribute => IsAttribute(attribute, names.Description));
        var texts = description is null ? [] : StringArguments(description);
        if (texts.Count < 2 || texts.Any(string.IsNullOrWhiteSpace))
        {
            findings.Add(Finding("tool_attributes", location, $"У инструмента «{tool.Name}» нет атрибута GpDescription с непустыми названиями по-русски и по-английски."));
        }

        var category = attributes.FirstOrDefault(attribute => IsAttribute(attribute, names.Category));
        var categoryText = category is null ? null : FirstString(category);
        if (string.IsNullOrWhiteSpace(categoryText))
        {
            findings.Add(Finding("tool_attributes", location, $"У инструмента «{tool.Name}» нет атрибута GpCategory с непустой категорией."));
        }

        var icon = attributes.FirstOrDefault(attribute => IsAttribute(attribute, names.Icon));
        if (string.IsNullOrWhiteSpace(icon is null ? null : FirstString(icon)))
        {
            findings.Add(Finding("tool_attributes", location, $"У инструмента «{tool.Name}» нет атрибута GpToolIcon с непустым значком."));
        }

        return new ToolDto(ReflectionName(tool), description is null ? null : FirstString(description), categoryText);
    }

    private void CheckParameters(CSharpCompilation compilation, INamedTypeSymbol tool, List<FindingDto> findings, CancellationToken cancellationToken)
    {
        var parameters = tool.GetMembers().OfType<IPropertySymbol>()
            .Where(property => property.DeclaredAccessibility == Accessibility.Public && !property.IsStatic && !property.IsIndexer
                && property.GetAttributes().Any(attribute => IsAttribute(attribute, names.Description)))
            .ToList();
        if (parameters.Count == 0)
        {
            return;
        }

        var assigned = AssignedInInnerExecute(compilation, tool, cancellationToken);
        foreach (var parameter in parameters)
        {
            var location = parameter.Locations[0];
            var getter = parameter.GetMethod;
            var setter = parameter.SetMethod;
            var readable = getter is not null && getter.DeclaredAccessibility == Accessibility.Public;
            var writable = setter is not null && !setter.IsInitOnly
                && setter.DeclaredAccessibility is Accessibility.Public or Accessibility.Private;
            if (!readable || !writable)
            {
                findings.Add(Finding("parameter_accessors", location, $"Параметр «{parameter.Name}»: чтение — открытое, запись — открытая у входного (set;) или закрытая у выходного (private set;)."));
                continue;
            }

            if (setter!.DeclaredAccessibility == Accessibility.Public && assigned.Contains(parameter))
            {
                findings.Add(Finding("output_parameter", location, $"Параметру «{parameter.Name}» присваивается значение в InnerExecute — это выходной параметр: запись должна быть private set."));
            }
        }
    }

    /// <summary>
    /// Properties of the tool assigned in its own <c>InnerExecute</c> body (<c>X = …</c>,
    /// <c>this.X = …</c>, compound assignment, also in nested lambdas and local functions).
    /// </summary>
    private static HashSet<IPropertySymbol> AssignedInInnerExecute(CSharpCompilation compilation, INamedTypeSymbol tool, CancellationToken cancellationToken)
    {
        var result = new HashSet<IPropertySymbol>(SymbolEqualityComparer.Default);
        var methods = tool.GetMembers(InnerExecuteName).OfType<IMethodSymbol>()
            .Where(method => method.Parameters.Length == 0 && !method.IsStatic);
        foreach (var reference in methods.SelectMany(method => method.DeclaringSyntaxReferences))
        {
            if (reference.GetSyntax(cancellationToken) is not MethodDeclarationSyntax syntax)
            {
                continue;
            }

            var body = (SyntaxNode?)syntax.Body ?? syntax.ExpressionBody;
            if (body is null)
            {
                continue;
            }

            var model = compilation.GetSemanticModel(syntax.SyntaxTree);
            foreach (var assignment in body.DescendantNodesAndSelf().OfType<AssignmentExpressionSyntax>())
            {
                var target = assignment.Left switch
                {
                    IdentifierNameSyntax name => name,
                    MemberAccessExpressionSyntax { Expression: ThisExpressionSyntax } access => access,
                    _ => (ExpressionSyntax?)null,
                };
                if (target is not null && model.GetSymbolInfo(target, cancellationToken).Symbol is IPropertySymbol property)
                {
                    result.Add(property.OriginalDefinition);
                }
            }
        }

        return result;
    }

    private static List<INamedTypeSymbol> ProjectClasses(CSharpCompilation compilation, IReadOnlyList<SyntaxTree> trees, CancellationToken cancellationToken)
    {
        var result = new List<INamedTypeSymbol>();
        var seen = new HashSet<INamedTypeSymbol>(SymbolEqualityComparer.Default);
        foreach (var tree in trees)
        {
            var model = compilation.GetSemanticModel(tree);
            foreach (var declaration in tree.GetRoot(cancellationToken).DescendantNodes().OfType<TypeDeclarationSyntax>())
            {
                if (model.GetDeclaredSymbol(declaration, cancellationToken) is { TypeKind: TypeKind.Class } type && seen.Add(type))
                {
                    result.Add(type);
                }
            }
        }

        return result;
    }

    private static bool IsAttribute(AttributeData attribute, string fullName) =>
        attribute.AttributeClass is { } type && FullName(type) == fullName;

    private static List<string?> StringArguments(AttributeData attribute) =>
        attribute.ConstructorArguments.Concat(attribute.NamedArguments.Select(argument => argument.Value))
            .Where(argument => argument.Kind == TypedConstantKind.Primitive && argument.Type?.SpecialType == SpecialType.System_String)
            .Select(argument => argument.Value as string)
            .ToList();

    private static string? FirstString(AttributeData attribute) =>
        attribute.ConstructorArguments
            .FirstOrDefault(argument => argument.Kind == TypedConstantKind.Primitive && argument.Type?.SpecialType == SpecialType.System_String)
            .Value as string;

    private static ExpressionSyntax? Unwrap(ExpressionSyntax? expression)
    {
        while (expression is ParenthesizedExpressionSyntax parenthesized)
        {
            expression = parenthesized.Expression;
        }

        return expression;
    }

    private static bool InheritsFrom(INamedTypeSymbol type, string baseName)
    {
        for (var current = type.BaseType; current is not null; current = current.BaseType)
        {
            if (FullName(current) == baseName)
            {
                return true;
            }
        }

        return false;
    }

    private static bool IsEffectivelyPublic(INamedTypeSymbol type)
    {
        for (INamedTypeSymbol? current = type; current is not null; current = current.ContainingType)
        {
            if (current.DeclaredAccessibility != Accessibility.Public)
            {
                return false;
            }
        }

        return true;
    }

    /// <summary>Namespace and metadata name (<c>Name`1</c> for generics, <c>Outer+Inner</c> for nested), as reflection shows it.</summary>
    internal static string ReflectionName(INamedTypeSymbol type)
    {
        var name = type.MetadataName;
        for (var outer = type.ContainingType; outer is not null; outer = outer.ContainingType)
        {
            name = $"{outer.MetadataName}+{name}";
        }

        var outermost = type;
        while (outermost.ContainingType is not null)
        {
            outermost = outermost.ContainingType;
        }

        return outermost.ContainingNamespace.IsGlobalNamespace
            ? name
            : $"{outermost.ContainingNamespace.ToDisplayString()}.{name}";
    }

    private static string FullName(INamedTypeSymbol type) => ReflectionName(type.OriginalDefinition);

    private static FindingDto Finding(string rule, Location? location, string message)
    {
        if (location is null || !location.IsInSource)
        {
            return new FindingDto(rule, null, null, null, message);
        }

        var start = location.GetLineSpan().StartLinePosition;
        return new FindingDto(rule, location.SourceTree!.FilePath, start.Line + 1, start.Character + 1, message);
    }
}
