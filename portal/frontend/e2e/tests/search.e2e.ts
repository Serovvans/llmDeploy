/**
 * Критерий 22 (docs/portal-design.md §10) — то, что видно со стенда: уточняющий вопрос находит
 * документ из предыдущего хода диалога, цитата источника дословна, недоступный реранкер поиску не
 * мешает. Остальное в критерии — разбиение, соседние фрагменты, пояснения, метрики — проверяют тесты
 * бэкенда и `portal eval-search` на ВМ.
 */
import { uniqueSuffix } from '../support/accounts';
import { textFile } from '../support/files';
import { expect, test } from '../support/fixtures';
import { addKbDocument, askChat, createDialog, eventsOf, sourcesOf, stubReply } from '../support/portal';

const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');

test('уточняющий вопрос без названия предмета находит документ из предыдущего хода; цитата дословна', async ({
  newMember,
}) => {
  const author = await newMember();
  const reader = await newMember();
  const mark = `zq${uniqueSuffix()}`;
  const text = `Договор аренды ${mark}: участок передаётся на сорок девять лет.`;
  const document = await addKbDocument(author.api, textFile(`договор-${mark}.txt`, text), { scope: 'shared' });

  const dialogId = await createDialog(reader.api, 'chat');
  const first = sourcesOf(await askChat(reader.api, dialogId, `Что сказано в договоре ${mark}?`, { collections: ['shared'] }));
  expect(first[0]?.document_id).toBe(document.id);
  // В уточнении нет ни одного слова документа: в большой общей базе стенда найти его можно только по
  // запросу, переписанному с учётом предыдущего вопроса.
  const followUp = sourcesOf(await askChat(reader.api, dialogId, `Уточни подробности ${CITE_ALL}`, { collections: ['shared'] }));
  const found = followUp.find((source) => source.document_id === document.id);
  // Ни строки «Документ: … / Раздел: …», ни пояснения в цитате нет.
  expect(found?.quote).toBe(text);
});

test('при недоступном реранкере поиск отвечает без ошибки', async ({ newMember }) => {
  const member = await newMember();
  const mark = `zq${uniqueSuffix()}`;
  const document = await addKbDocument(member.api, textFile(`справка-${mark}.txt`, `Справка ${mark} о границах участка.`), {
    scope: 'personal',
  });
  const dialogId = await createDialog(member.api, 'chat');
  const events = await askChat(member.api, dialogId, `${mark} [[stub:rerank-error]]`, { collections: ['personal'] });
  expect(eventsOf(events, 'error')).toEqual([]);
  expect(events.at(-1)?.event).toBe('done');
  expect(sourcesOf(events).map((source) => source.document_id)).toEqual([document.id]);
});
