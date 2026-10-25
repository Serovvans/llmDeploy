using System;
using eLiteGIS.Geoprocessing;

namespace {{namespace}}
{
    public sealed class GpToolFactory : IGpToolFactory
    {
        public Type[] GetToolTypes()
        {
            return new Type[] { {{tool_types}} };
        }
    }
}
