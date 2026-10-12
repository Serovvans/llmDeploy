import { Button, Checkbox, Link, TokenInput, TokenInputType, Tooltip } from '@skbkontur/react-ui';
import { useEffect, useId, useRef, useState } from 'react';

import { api } from '../../api/client';
import type { KbScope } from '../../api/types';
import { KB_SCOPES, knowledgeChoice, orderScopes } from '../../chat/knowledge';
import { texts } from '../../texts';
import styles from './KnowledgePicker.module.css';

const t = texts.chat.composer.knowledge;
const FOCUSABLE = 'input:not([disabled]), textarea:not([disabled]), button:not([disabled]), [tabindex="0"]';

interface KnowledgePickerProps {
  collections: KbScope[];
  tags: string[];
  /** Сколько тегов можно выбрать в одном вопросе; `null`, пока нет конфигурации, — предел проверит сервер. */
  maxTags: number | null;
  onCollectionsChange: (collections: KbScope[]) => void;
  onTagsChange: (tags: string[]) => void;
}

/**
 * Выбор баз знаний и тегов (концепция §4.1): кнопка в строке управления и панель над ней.
 * Выбор действует сразу и относится к следующему отправляемому вопросу.
 */
export function KnowledgePicker({ collections, tags, maxTags, onCollectionsChange, onTagsChange }: KnowledgePickerProps) {
  const [open, setOpen] = useState(false);
  const [tagsFailed, setTagsFailed] = useState(false);
  const button = useRef<Button>(null);
  const panel = useRef<HTMLDivElement>(null);
  const tagInput = useRef<TokenInput<string>>(null);
  const noteId = useId();
  const noScope = collections.length === 0;
  const limitReached = maxTags !== null && tags.length >= maxTags;

  // Клавиатура панели (концепция §9): Esc закрывает и возвращает фокус на кнопку, Tab из панели не выходит.
  // Слушатель — на документе в фазе перехвата: поле тегов библиотеки не пропускает нажатия выше себя.
  useEffect(() => {
    if (!open) {
      return;
    }
    panel.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        setOpen(false);
        button.current?.focus();
        return;
      }
      const stops = Array.from(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
      if (event.key !== 'Tab' || stops.length === 0) {
        return;
      }
      const edge = event.shiftKey ? stops[0] : stops.at(-1);
      if (document.activeElement === edge || !panel.current?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? stops.at(-1) : stops[0])?.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open]);

  const toggleScope = (scope: KbScope, checked: boolean) =>
    onCollectionsChange(orderScopes(checked ? [...collections, scope] : collections.filter((item) => item !== scope)));

  const loadTags = async (query: string): Promise<string[]> => {
    if (limitReached) {
      return [];
    }
    try {
      const { items } = await api.listKbTags(collections, query.trim());
      setTagsFailed(false);
      return items;
    } catch {
      // Подсказки не загрузились: под полем — заметка с «Повторить»; базы выбираются как обычно.
      setTagsFailed(true);
      return [];
    }
  };

  const renderPanel = () => (
    <div ref={panel} className={styles.panel} role="dialog" aria-label={t.label}>
      <p className={styles.title}>{t.title}</p>
      <p className={styles.muted}>{t.text}</p>
      <div className={styles.scopes}>
        {KB_SCOPES.map((scope) => (
          <Checkbox key={scope} checked={collections.includes(scope)} onValueChange={(checked) => toggleScope(scope, checked)}>
            {t.scopes[scope]} — {t.scopeHints[scope]}
          </Checkbox>
        ))}
      </div>
      <p className={styles.title}>{t.tagsTitle}</p>
      <TokenInput<string>
        ref={tagInput}
        type={TokenInputType.WithReference}
        width="100%"
        aria-label={t.tagsTitle}
        aria-describedby={noteId}
        placeholder={t.tagsPlaceholder}
        disabled={noScope}
        selectedItems={tags}
        getItems={loadTags}
        // Без строки «не найдено» список подсказок не открывается: при сбое и при достигнутом пределе его нет.
        renderNotFound={() => (tagsFailed || limitReached ? null : t.noTag)}
        // Достигнут предел — новый тег не принимается; убрать тег можно всегда.
        onValueChange={(next) => (next.length < tags.length || maxTags === null || next.length <= maxTags) && onTagsChange(next)}
      />
      <div id={noteId} className={styles.notes}>
        {tagsFailed && !noScope && (
          <p className={styles.failed} role="alert">
            {t.tagsFailed}{' '}
            <Link
              component="button"
              onClick={() => {
                setTagsFailed(false);
                tagInput.current?.focus();
              }}
            >
              {texts.common.retry}
            </Link>
          </p>
        )}
        {limitReached && maxTags !== null && <p className={styles.muted}>{t.tagsLimit(maxTags)}</p>}
        <p className={styles.muted}>{noScope ? t.tagsNeedScope : t.tagsNote}</p>
      </div>
    </div>
  );

  return (
    <Tooltip
      trigger={open ? 'opened' : 'closed'}
      pos="top left"
      allowedPositions={['top left']}
      closeButton={false}
      render={renderPanel}
      onCloseRequest={() => setOpen(false)}
    >
      <Button
        ref={button}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`${t.label}: ${knowledgeChoice(collections, tags.length, ', ')}`}
        onClick={() => setOpen(!open)}
      >
        {t.label}: {knowledgeChoice(collections, tags.length, ' · ')}
      </Button>
    </Tooltip>
  );
}
