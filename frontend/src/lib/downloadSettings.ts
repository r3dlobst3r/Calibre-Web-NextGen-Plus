/* The download service's settings, as Admin → Downloads draws them.
 *
 * Shelfmark describes its settings as tabs of typed fields with conditional
 * visibility, and CWNG renders that description rather than copying each form.
 * These rules mirror Shelfmark's own settings screen so a field appears, locks
 * and saves exactly as it would there.
 *
 * Import-free so the node unit lane can exercise it directly.
 */

export interface ShowWhenCondition {
  field: string;
  value?: string | string[] | boolean;
  notEmpty?: boolean;
}

export type ShowWhen = ShowWhenCondition | ShowWhenCondition[];

export interface SettingsOption {
  value: string;
  label: string;
  description?: string;
  childOf?: string;
}

export interface OrderableOption {
  id: string;
  label: string;
  description?: string;
  disabledReason?: string;
  isLocked?: boolean;
  isPinned?: boolean;
}

export interface OrderableItem {
  id: string;
  enabled: boolean;
}

export interface TableColumn {
  key: string;
  label: string;
  type: 'text' | 'password' | 'select' | 'multiselect' | 'checkbox' | 'path';
  placeholder?: string;
  options?: SettingsOption[];
  defaultValue?: string | string[] | boolean;
}

export interface SettingsField {
  key: string;
  type: string;
  label?: string;
  title?: string;
  description?: string;
  value?: unknown;
  /** A stored password the server withheld: blank means "keep it". */
  hasValue?: boolean;
  fromEnv?: boolean;
  disabled?: boolean;
  disabledReason?: string;
  showWhen?: ShowWhen;
  disabledWhen?: { field: string; value: string | string[] | boolean; reason?: string };
  requiresRestart?: boolean;
  hiddenInUi?: boolean;
  universalOnly?: boolean;
  options?: SettingsOption[] | OrderableOption[];
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  columns?: TableColumn[];
  addLabel?: string;
  emptyMessage?: string;
  style?: 'default' | 'primary' | 'danger';
  linkUrl?: string;
  linkText?: string;
  variant?: string;
  component?: string;
  boundFields?: SettingsField[];
}

export interface SettingsTab {
  name: string;
  displayName: string;
  icon?: string | null;
  order?: number;
  group?: string | null;
  fields: SettingsField[];
}

export type SettingsValues = Record<string, unknown>;

/** Fields that hold a value (headings, buttons and custom widgets do not). */
export function isValueField(field: SettingsField): boolean {
  return !['HeadingField', 'ActionButton', 'CustomComponentField'].includes(field.type);
}

function valueFields(fields: SettingsField[]): SettingsField[] {
  return fields.flatMap((field) => field.type === 'CustomComponentField'
    ? valueFields(field.boundFields ?? [])
    : isValueField(field) ? [field] : []);
}

/** Every current value across all tabs: a condition may name another tab's field. */
export function collectValues(tabs: SettingsTab[]): SettingsValues {
  const values: SettingsValues = {};
  for (const tab of tabs) {
    for (const field of valueFields(tab.fields)) values[field.key] = field.value;
  }
  return values;
}

function comparable(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return undefined;
}

function conditionHolds(condition: ShowWhenCondition, values: SettingsValues): boolean {
  const current = values[condition.field];
  if (condition.notEmpty) {
    if (Array.isArray(current)) return current.length > 0;
    return current !== undefined && current !== null && current !== '';
  }
  if (Array.isArray(condition.value)) {
    const value = comparable(current);
    return value !== undefined && condition.value.includes(value);
  }
  return current === condition.value;
}

/** Universal search uses metadata providers; some fields only matter there. */
export function isUniversalMode(values: SettingsValues): boolean {
  return (values.SEARCH_MODE ?? 'universal') === 'universal';
}

export function isFieldVisible(field: SettingsField, values: SettingsValues): boolean {
  if (field.hiddenInUi) return false;
  if (field.universalOnly && !isUniversalMode(values)) return false;
  if (!field.showWhen) return true;
  const conditions = Array.isArray(field.showWhen) ? field.showWhen : [field.showWhen];
  return conditions.every((condition) => conditionHolds(condition, values));
}

/** Locked by the environment, statically, or by another field's value. */
export function fieldDisabled(field: SettingsField, values: SettingsValues): { disabled: boolean; reason?: string } {
  if (field.fromEnv) return { disabled: true, reason: 'fromEnv' };
  if (field.disabled) return { disabled: true, reason: field.disabledReason || undefined };
  const rule = field.disabledWhen;
  if (!rule) return { disabled: false };
  const current = values[rule.field];
  const hit = Array.isArray(rule.value)
    ? rule.value.includes(comparable(current) ?? '\u0000')
    : current === rule.value;
  return hit ? { disabled: true, reason: rule.reason } : { disabled: false };
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** What saving a tab sends: only fields the administrator changed, and a
 *  password only when one was typed (blank keeps the stored secret). Fields set
 *  by the environment are never sent; Shelfmark would ignore them anyway. */
export function changedValues(tab: SettingsTab, saved: SettingsValues, current: SettingsValues): SettingsValues {
  const changes: SettingsValues = {};
  for (const field of valueFields(tab.fields)) {
    if (field.fromEnv) continue;
    const value = current[field.key];
    if (field.type === 'PasswordField') {
      if (typeof value === 'string' && value !== '') changes[field.key] = value;
      continue;
    }
    if (!same(value, saved[field.key])) changes[field.key] = value;
  }
  return changes;
}

/** Shelfmark's tab order, grouped under its own section names when it sends them. */
export function orderTabs(tabs: SettingsTab[]): SettingsTab[] {
  return [...tabs].sort((a, b) => (a.order ?? 100) - (b.order ?? 100) || a.displayName.localeCompare(b.displayName));
}

/** An orderable list's current order, adding any option the stored value is missing. */
export function orderedItems(value: unknown, options: OrderableOption[]): OrderableItem[] {
  const stored = Array.isArray(value)
    ? value.filter((item): item is OrderableItem => !!item && typeof item === 'object'
        && typeof (item as OrderableItem).id === 'string')
    : [];
  const known = new Set(options.map((option) => option.id));
  const items = stored.filter((item) => known.has(item.id)).map((item) => ({ id: item.id, enabled: !!item.enabled }));
  for (const option of options) {
    if (!items.some((item) => item.id === option.id)) items.push({ id: option.id, enabled: false });
  }
  return items;
}

/** Move one item in an orderable list; pinned items keep their place. */
export function moveItem(items: OrderableItem[], index: number, offset: -1 | 1, options: OrderableOption[]): OrderableItem[] {
  const target = index + offset;
  if (target < 0 || target >= items.length) return items;
  const pinned = (id: string) => options.find((option) => option.id === id)?.isPinned;
  if (pinned(items[index].id) || pinned(items[target].id)) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** A new table row with each column's default. */
export function newTableRow(columns: TableColumn[]): Record<string, unknown> {
  const row: Record<string, unknown> = {};
  for (const column of columns) {
    row[column.key] = column.defaultValue ?? (column.type === 'checkbox' ? false
      : column.type === 'multiselect' ? [] : '');
  }
  return row;
}
