using System;
using eLiteGIS.Geoprocessing;
using eLiteGIS.Utilities;
{{usings}}

namespace {{namespace}}
{
    [GpToolIcon({{icon}})]
    [GpCategory({{category}})]
    [GpDescription({{title_ru}}, {{title_en}})]
    public sealed class {{class_name}} : GpTool
    {
{{inputs}}
{{outputs}}
{{database}}

        public override void ValidateInput()
        {
{{validate}}
        }

        public override void InnerExecute()
        {
            // Логика инструмента: прочитайте входные параметры и заполните выходные.
        }
    }
}
