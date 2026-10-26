// Как CoGIS подключает плагин, документация не описывает: проверьте заготовку на тестовом приложении.
// Плагин ищет объекты в слое, приближает карту к первому найденному, подсвечивает его
// и сообщает, сколько объектов нашлось.

// Замените на свои: адрес сервиса карты и номер слоя в нём.
var serviceUrl = "https://cogis.example/arcgis/rest/services/Parcels/MapServer";
var layerId = 0;

// Замените на своё: условие отбора — выражение SQL по полям слоя.
var whereClause = "status = 'Аренда'";

var layer = api.getLayer(serviceUrl, layerId, "map");

// Результат поиска может прийти не сразу — дожидаемся его.
Promise.resolve(api.searchInLayer(layer, whereClause)).then(function (items) {
  var found = items || [];
  if (found.length === 0) {
    api.showAlert("Поиск объектов", "Объекты по условию не найдены.");
    return;
  }
  var first = found[0];
  api.goTo(first.geometry);
  api.highlight(first.geometry);
  api.showAlert("Поиск объектов", "Найдено объектов: " + found.length + ".");
});
