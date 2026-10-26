// Описание api клиента CoGIS по документации заказчика (п. 7.10) — для подсказок
// редактора, помощника и заглушки предпросмотра портала (docs/portal-api.md §7.5.16).
// Типы параметров и результатов документация не задаёт — везде any.
// Известные расхождения документации: фильтр слоя назван двумя именами —
// addFilterToLayer в списке функций и addAttributeFilterToLayer в примерах, объявлены
// оба; createSimpleLineSymbol перечислен в документации дважды — объявлен один раз.

declare const api: CogisApi;

interface CogisApi {
  /**
   * Создаёт прямоугольную область по границам в заданной системе координат.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createExtent(xmin: any, xmax: any, ymin: any, ymax: any, sr: any): any;
  /**
   * Создаёт точку по координатам в заданной системе координат.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createPoint(x: any, y: any, sr: any): any;
  /**
   * Возвращает центр геометрии.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  getCentroid(geometry: any): any;
  /**
   * Возвращает систему координат по коду, например 4326, или по описанию WKT.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  getSpatialReference(id: any): any;
  /**
   * Пересчитывает геометрию в другую систему координат.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  project(geometry: any, sr: any): any;
  /**
   * Приближает карту к геометрии; отступ от краёв — в процентах.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   * @param marginInPercent По умолчанию 20.
   */
  goTo(geometry: any, maxScale: any, minScale: any, marginInPercent?: any): any;
  /**
   * Подсвечивает геометрию на карте.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   * @param withLines По умолчанию false.
   */
  highlight(geometry: any, withLines?: any): any;
  /**
   * Ставит метку в точке геометрии.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  showPushpin(geometry: any, icon: any): any;
  /**
   * Находит слой проекта по адресу сервиса и номеру слоя. existsIn — где искать: map, legend, search или attributeTable.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  getLayer(serviceUrl: any, layerId: any, existsIn: any): any;
  /**
   * Ищет объекты слоя по условию, номерам или геометрии; возвращает объекты с геометрией и атрибутами в системе координат карты. Работает, если запросы к слою разрешены в настройках.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  searchInLayer(layer: any, whereClause: any, objectIds: any, geometry: any): any;
  /**
   * Ищет адрес первым геокодером проекта.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  geocodeAddress(address: any, filters: any): any;
  /**
   * Добавляет фильтр к слою.
   *
   * В документации CoGIS есть оба имени: addFilterToLayer — в списке функций, addAttributeFilterToLayer — в примерах. Какое работает в вашей версии, проверьте в CoGIS.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  addFilterToLayer(layer: any, criterion: any): any;
  /**
   * Убирает фильтр со слоя.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  removeFilterInLayer(layer: any, filter: any): any;
  /**
   * Убирает все фильтры слоя.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  clearAllFiltersInLayer(layer: any): any;
  /**
   * Ищет по тексту, как строка поиска приложения.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  search(text: any): any;
  /**
   * Открывает сведения об объекте.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   * @param goTo По умолчанию true.
   * @param calloutOnly По умолчанию false.
   */
  openIdentify(feature: any, goTo?: any, calloutOnly?: any): any;
  /**
   * Показывает объекты в точке или области.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  identify(geometry: any): any;
  /**
   * Открывает таблицу атрибутов слоя; высота — в пикселях или строкой, например "30%".
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  openAttributeTable(layer: any, panelHeight: any): any;
  /**
   * Открывает виджет приложения по имени.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  openWidget(name: any): any;
  /**
   * Показывает окно с заголовком и сообщением.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  showAlert(title: any, message: any): any;
  /**
   * Строит зоны доступности от точек; пустой список точек убирает зоны.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  constructServiceArea(points: any, cost: any, breaks: any, restrictions: any): any;
  /**
   * Строит маршрут через точки; пустой список точек убирает маршрут.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  findRoute(points: any, cost: any, restrictions: any): any;
  /**
   * Рисует геометрию на карте выбранным символом; геометрия — в WGS 84.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  addGraphic(geometryInWGS84: any, symbol: any): any;
  /**
   * Убирает нарисованную геометрию.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  deleteGraphic(graphic: any): any;
  /**
   * Создаёт цвет из красной, зелёной и синей составляющих и прозрачности.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createColor(r: any, g: any, b: any, alpha: any): any;
  /**
   * Символ точки: цвет, размер, обводка, вид.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createSimplePointSymbol(color: any, size: any, outline: any, type: any): any;
  /**
   * Символ точки картинкой.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createPicturePointSymbol(icon: any, iconSize: any, locationX: any, locationY: any): any;
  /**
   * Символ линии: цвет, толщина, вид.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createSimpleLineSymbol(color: any, width: any, type: any): any;
  /**
   * Символ заливки: цвет, обводка, вид.
   *
   * Документация CoGIS, п. 7.10. Что возвращает — не описано.
   */
  createSimpleFillSymbol(color: any, outline: any, type: any): any;
  /**
   * Фильтрует слой по значениям поля.
   *
   * В документации CoGIS есть оба имени: addFilterToLayer — в списке функций, addAttributeFilterToLayer — в примерах. Какое работает в вашей версии, проверьте в CoGIS.
   *
   * Есть только в примерах документации CoGIS, в списке функций её нет.
   * @param comparison В примерах документации — 'Equal'.
   */
  addAttributeFilterToLayer(layer: any, field: any, values: any, comparison: any): any;
  /**
   * Убирает фильтр по значениям поля.
   *
   * Есть только в примерах документации CoGIS, в списке функций её нет.
   * @param comparison В примерах документации — 'Equal'.
   */
  removeAttributeFilterToLayer(layer: any, field: any, values: any, comparison: any): any;
  /**
   * Формирует отчёт по шаблону для слоя.
   *
   * Есть только в примерах документации CoGIS, в списке функций её нет.
   */
  generateReport(template: any, source: any, options: any): any;
}

// Хуки карточки объекта: функции окна, которые задаёт код сотрудника (п. 7.9.3–7.9.4).
interface Window {
  /**
   * Проверка перед сохранением объекта карточки.
   * @param object Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   * @param project Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   */
  checkBeforeSave?: (object: any, project: any) => any;
  /**
   * Решает, показывать ли кнопку карточки.
   * @param object Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   * @param button Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   * @param project Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   */
  checkButtonVisibilityInCard?: (object: any, button: any, project: any) => any;
  /**
   * Проверка перед запуском задачи кнопкой карточки.
   * @param object Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   * @param button Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   * @param project Объект карточки CoGIS: свойства не описаны в документации, смотрите примеры п. 7.9.3.
   */
  checkBeforeExecuteButtonInCard?: (object: any, button: any, project: any) => any;
}
