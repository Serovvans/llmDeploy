import { Component } from 'react';
import { useNavigate } from 'react-router-dom';

import { EmptyState } from '../components/EmptyState';
import { usePageTitle } from '../hooks/usePageTitle';
import { texts } from '../texts';

const t = texts.service;

/** «Страница не найдена» — любой адрес вне карты экранов (концепция §5.12). */
export function NotFoundScreen() {
  usePageTitle(t.notFound.title);
  const navigate = useNavigate();
  return (
    <EmptyState
      headingLevel="h1"
      title={t.notFound.title}
      text={t.notFound.text}
      action={{ label: texts.common.goToChat, onClick: () => navigate('/chat') }}
    />
  );
}

/** «Портал не отвечает» — первый запрос сессии не получил ответа. */
export function UnavailableScreen({ onRetry }: { onRetry: () => void }) {
  usePageTitle(t.unavailable.title);
  return (
    <EmptyState
      headingLevel="h1"
      title={t.unavailable.title}
      text={t.unavailable.text}
      action={{ label: t.unavailable.action, onClick: onRetry }}
    />
  );
}

/** «Что-то сломалось» — сбой интерфейса при отрисовке. */
export class CrashBoundary extends Component<{ children: React.ReactNode }, { crashed: boolean }> {
  state = { crashed: false };

  static getDerivedStateFromError(): { crashed: boolean } {
    return { crashed: true };
  }

  render() {
    if (!this.state.crashed) {
      return this.props.children;
    }
    return (
      <EmptyState
        headingLevel="h1"
        title={t.crash.title}
        text={t.crash.text}
        action={{ label: t.crash.action, onClick: () => window.location.reload() }}
      />
    );
  }
}
