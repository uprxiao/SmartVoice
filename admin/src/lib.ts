import { useState } from 'react';
import { lastShanghaiDays } from './date-range';

export function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function lastDays(days: number): { from: string; to: string } {
  return lastShanghaiDays(days);
}

export function useRange(days = 7) {
  const initial = lastDays(days);
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  return { from, to, setFrom, setTo };
}

export function formatNumber(value: number): string {
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;
  return value.toLocaleString('zh-CN');
}

export function formatAudio(ms: number): string {
  if (ms <= 0) return '0 秒';
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${(minutes / 60).toFixed(1)} 小时`;
}

export function formatDateTime(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}
