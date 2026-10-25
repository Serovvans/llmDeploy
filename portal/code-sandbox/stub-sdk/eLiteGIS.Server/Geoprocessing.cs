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

/// <summary>Category of a tool in the CoGIS tool list.</summary>
[AttributeUsage(AttributeTargets.Class)]
public sealed class GpCategoryAttribute : Attribute
{
    /// <summary>Creates the category.</summary>
    /// <param name="name">Category name.</param>
    public GpCategoryAttribute(string name)
    {
    }
}

/// <summary>Error that CoGIS shows to the user as is.</summary>
public class UserException : Exception
{
    /// <summary>Creates the error.</summary>
    /// <param name="exceptionMessage">Text for the user.</param>
    public UserException(string exceptionMessage)
        : base(exceptionMessage)
    {
    }
}
