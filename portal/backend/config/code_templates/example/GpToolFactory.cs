using System;
using eLiteGIS.Geoprocessing;

namespace {{namespace}}
{
    // CoGIS создаёт этот класс по полному имени и берёт у него список инструментов библиотеки.
    public sealed class GpToolFactory : IGpToolFactory
    {
        public Type[] GetToolTypes()
        {
            return new Type[] { typeof(AddContactTool) };
        }
    }
}
