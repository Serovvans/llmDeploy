import type { DialogMemory } from '../../memory/useDialogMemory';
import { texts } from '../../texts';

const t = texts.memory;

/** Подсказка с процентом — одна у индикатора и у полосы в панели (концепция §4.9). */
export function levelHint(memory: DialogMemory): string {
  if (memory.status === 'failed') {
    return t.unavailableHint;
  }
  if (memory.status === 'new') {
    return t.percentHint(0);
  }
  if (memory.truncated) {
    return t.overflowHint;
  }
  const estimate = memory.memory;
  if (!estimate) {
    return '';
  }
  if (estimate.level === 'overflow') {
    // «Переполнена» ниже 100 % — из-за предела картинок в одном запросе (контракт §5.10.2).
    return estimate.percent >= 100 ? t.overflowHint : t.imagesHint(estimate.percent);
  }
  return t.percentHint(estimate.percent);
}

/** Заполнение полосы: «переполнена» — полная полоса. */
export function levelPercent(memory: DialogMemory): number {
  if (memory.level === 'overflow') {
    return 100;
  }
  return Math.min(100, Math.max(0, memory.memory?.percent ?? 0));
}

export function isWarningLevel(memory: DialogMemory): boolean {
  return memory.level === 'almost_full' || memory.level === 'overflow';
}
