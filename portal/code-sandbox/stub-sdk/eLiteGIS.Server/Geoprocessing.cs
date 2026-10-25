namespace eLiteGIS.Geoprocessing;

/// <summary>Factory that CoGIS asks for the tool types of a library.</summary>
public interface IGpToolFactory
{
    /// <summary>Returns the tool types of the library.</summary>
    Type[] GetToolTypes();
}

/// <summary>Base class of a geoprocessing tool.</summary>
public abstract class GpTool
{
    /// <summary>Checks the input parameters; input errors are reported with <c>UserException</c>.</summary>
    public virtual void ValidateInput()
    {
    }

    /// <summary>Runs the tool.</summary>
    public abstract void InnerExecute();
}

/// <summary>Russian and English description of a tool or a parameter.</summary>
[AttributeUsage(AttributeTargets.Class | AttributeTargets.Property)]
public sealed class GpDescriptionAttribute : Attribute
{
    /// <summary>Creates the description.</summary>
    /// <param name="description">Russian text.</param>
    /// <param name="englishDescription">English text.</param>
    public GpDescriptionAttribute(string description, string? englishDescription = null)
    {
    }
}

/// <summary>Category of a tool in the CoGIS tool list.</summary>
[AttributeUsage(AttributeTargets.Class)]
public sealed class GpCategoryAttribute : Attribute
{
    /// <summary>Creates the category.</summary>
    /// <param name="category">Category name.</param>
    public GpCategoryAttribute(string category)
    {
    }
}

/// <summary>Font Awesome icon of a tool.</summary>
[AttributeUsage(AttributeTargets.Class)]
public sealed class GpToolIconAttribute : Attribute
{
    /// <summary>Creates the icon.</summary>
    /// <param name="icon">Font Awesome classes, for example <c>fa fa-envelope</c>.</param>
    public GpToolIconAttribute(string icon)
    {
    }
}

/// <summary>Marks a required input parameter.</summary>
[AttributeUsage(AttributeTargets.Property)]
public sealed class GpRequiredAttribute : Attribute
{
}
