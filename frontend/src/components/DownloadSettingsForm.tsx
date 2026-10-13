import { useEffect, useId, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react';
import {
  changedValues,
  collectValues,
  fieldDisabled,
  isFieldVisible,
  isValueField,
  moveItem,
  newTableRow,
  orderedItems,
  type OrderableItem,
  type OrderableOption,
  type SettingsField,
  type SettingsOption,
  type SettingsTab,
  type SettingsValues,
  type TableColumn,
} from '../lib/downloadSettings';
import { runDownloadSettingsAction, saveDownloadSettings, type DownloadSettingsResult } from '../lib/downloads';
import { useT } from '../lib/i18n';
import { useAnnouncer } from '../lib/a11y/announcer';
import styles from './DownloadSettingsForm.module.css';

type Change = (key: string, value: unknown) => void;

/** One tab of the download service's settings, drawn from Shelfmark's own
 *  description of it. Values from every tab are kept because a field's
 *  visibility can depend on a setting in another tab. */
export function DownloadSettingsForm({ tabs, tab }: { tabs: SettingsTab[]; tab: SettingsTab }) {
  const t = useT();
  const announce = useAnnouncer();
  const queryClient = useQueryClient();
  const saved = useMemo(() => collectValues(tabs), [tabs]);
  const [values, setValues] = useState<SettingsValues>(saved);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [actionResults, setActionResults] = useState<Record<string, { ok: boolean; message: string }>>({});

  // A fresh read (after saving, or another tab's save) replaces the draft.
  useEffect(() => { setValues(saved); }, [saved]);
  useEffect(() => { setResult(null); setActionResults({}); }, [tab.name]);

  const change: Change = (key, value) => setValues((current) => ({ ...current, [key]: value }));
  const changes = changedValues(tab, saved, values);
  const dirty = Object.keys(changes).length > 0;
  const needsRestart = tab.fields.some((field) => field.requiresRestart && field.key in changes);

  const save = useMutation({
    mutationFn: () => saveDownloadSettings(tab.name, changes),
    onSuccess: (response: DownloadSettingsResult) => {
      const ok = response.success !== false;
      const message = response.message || (ok ? t('Saved.') : t('The settings were not saved.'));
      setResult({ ok, message: ok && needsRestart
        ? t('Saved. Restart the download service for this to take effect.') : message });
      announce(message);
      if (ok) void queryClient.invalidateQueries({ queryKey: ['admin', 'downloads', 'settings'] });
    },
    onError: (error) => setResult({ ok: false, message: error instanceof Error && error.message
      ? error.message : t('The settings were not saved.') }),
  });

  const action = useMutation({
    mutationFn: (key: string) => runDownloadSettingsAction(tab.name, key, tabValues(tab, values)),
    onSuccess: (response, key) => {
      const ok = response.success !== false;
      const message = response.message || (ok ? t('Done.') : t('That did not work.'));
      setActionResults((current) => ({ ...current, [key]: { ok, message } }));
      announce(message);
    },
    onError: (error, key) => setActionResults((current) => ({ ...current, [key]: {
      ok: false, message: error instanceof Error && error.message ? error.message : t('That did not work.') } })),
  });

  const visible = tab.fields.filter((field) => isFieldVisible(field, values)
    && !(field.type === 'CustomComponentField'));

  return (
    <form className={styles.form} onSubmit={(event) => { event.preventDefault(); if (dirty) save.mutate(); }}>
      {visible.length === 0 && <p className={styles.hint}>{t('Nothing to set here right now.')}</p>}
      {visible.map((field) => (
        <FieldRow key={field.key} field={field} values={values} change={change}
          runAction={(key) => action.mutate(key)} actionPending={action.isPending && action.variables === field.key}
          actionResult={actionResults[field.key]} />
      ))}
      {tab.fields.some((field) => isValueField(field)) && (
        <div className={styles.footer}>
          <button type="submit" className={styles.primary} disabled={!dirty || save.isPending}>
            {save.isPending ? t('Saving…') : t('Save')}
          </button>
          {dirty && !save.isPending && (
            <button type="button" className={styles.secondary} onClick={() => setValues(saved)}>{t('Discard changes')}</button>
          )}
          {result && <p className={result.ok ? styles.ok : styles.bad} role="status">{result.message}</p>}
        </div>
      )}
    </form>
  );
}

/** The values an action (e.g. "Test connection") is tried against: this tab's
 *  current draft, so an administrator can test before saving. */
function tabValues(tab: SettingsTab, values: SettingsValues): SettingsValues {
  const result: SettingsValues = {};
  for (const field of tab.fields) {
    if (isValueField(field) && !(field.type === 'PasswordField' && values[field.key] === '')) {
      result[field.key] = values[field.key];
    }
  }
  return result;
}

function FieldRow({ field, values, change, runAction, actionPending, actionResult }: {
  field: SettingsField;
  values: SettingsValues;
  change: Change;
  runAction: (key: string) => void;
  actionPending: boolean;
  actionResult?: { ok: boolean; message: string };
}) {
  const t = useT();
  const id = useId();
  const describedBy = `${id}-description`;
  const { disabled, reason } = fieldDisabled(field, values);
  const description = field.description ? <p id={describedBy} className={styles.description}>{field.description}</p> : null;
  const lock = disabled ? (
    <p className={styles.hint}>{reason === 'fromEnv'
      ? t('Set by the server environment.') : reason || t('Not available with the current settings.')}</p>
  ) : null;

  if (field.type === 'HeadingField') {
    return (
      <div className={styles.heading}>
        <h3>{field.title}</h3>
        {field.description && <p className={styles.description}>{field.description}</p>}
        {field.linkUrl && <a href={field.linkUrl} target="_blank" rel="noreferrer noopener" className={styles.link}>{field.linkText || field.linkUrl}</a>}
      </div>
    );
  }

  if (field.type === 'ActionButton') {
    return (
      <div className={styles.row}>
        <button type="button" className={field.style === 'primary' ? styles.primary : field.style === 'danger' ? styles.danger : styles.secondary}
          disabled={disabled || actionPending} onClick={() => runAction(field.key)} aria-describedby={field.description ? describedBy : undefined}>
          {actionPending ? t('Working…') : field.label}
        </button>
        {description}
        {actionResult && <p className={actionResult.ok ? styles.ok : styles.bad} role="status">{actionResult.message}</p>}
      </div>
    );
  }

  const value = values[field.key];
  const common = { id, disabled, 'aria-describedby': field.description ? describedBy : undefined };
  let control: JSX.Element | null = null;
  switch (field.type) {
    case 'CheckboxField':
      return (
        <div className={styles.row}>
          <label className={styles.checkbox}>
            <input type="checkbox" {...common} checked={value === true} onChange={(event) => change(field.key, event.target.checked)} />
            <span>{field.label}</span>
          </label>
          {description}{lock}
        </div>
      );
    case 'TextField':
      control = <input type="text" className={styles.input} {...common} value={typeof value === 'string' ? value : ''}
        placeholder={field.placeholder} onChange={(event) => change(field.key, event.target.value)} />;
      break;
    case 'PasswordField':
      control = <input type="password" className={styles.input} {...common} autoComplete="new-password"
        value={typeof value === 'string' ? value : ''}
        placeholder={field.hasValue ? t('Saved. Leave blank to keep it.') : field.placeholder}
        onChange={(event) => change(field.key, event.target.value)} />;
      break;
    case 'NumberField':
      control = <input type="number" className={styles.input} {...common} min={field.min} max={field.max} step={field.step}
        value={typeof value === 'number' || typeof value === 'string' ? String(value) : ''}
        onChange={(event) => change(field.key, event.target.value === '' ? '' : Number(event.target.value))} />;
      break;
    case 'SelectField':
      control = (
        <select className={styles.input} {...common} value={typeof value === 'string' ? value : ''}
          onChange={(event) => change(field.key, event.target.value)}>
          {!(field.options as SettingsOption[] | undefined)?.some((option) => option.value === value) && <option value="">{t('Choose…')}</option>}
          {((field.options as SettingsOption[] | undefined) ?? []).map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      );
      break;
    case 'MultiSelectField':
      control = <MultiSelect field={field} value={value} disabled={disabled} change={change} />;
      break;
    case 'TagListField':
      control = <textarea className={styles.textarea} {...common} rows={3}
        value={Array.isArray(value) ? value.join('\n') : ''} placeholder={field.placeholder || t('One per line')}
        onChange={(event) => change(field.key, event.target.value.split('\n').map((line) => line.trim()).filter(Boolean))} />;
      break;
    case 'OrderableListField':
      control = <OrderableList field={field} value={value} disabled={disabled} change={change} />;
      break;
    case 'TableField':
      control = <Table field={field} value={value} disabled={disabled} change={change} />;
      break;
    default:
      return null;
  }
  return (
    <div className={styles.row}>
      <label htmlFor={id} className={styles.label}>
        {field.label}{field.requiresRestart && <span className={styles.badge}>{t('Restart needed')}</span>}
      </label>
      {control}
      {description}{lock}
    </div>
  );
}

function MultiSelect({ field, value, disabled, change }: { field: SettingsField; value: unknown; disabled: boolean; change: Change }) {
  const selected = new Set(Array.isArray(value) ? value.map(String) : []);
  const options = (field.options as SettingsOption[] | undefined) ?? [];
  return (
    <div className={styles.choices} role="group" aria-label={field.label}>
      {options.map((option) => (
        <label key={option.value} className={styles.checkbox}>
          <input type="checkbox" disabled={disabled} checked={selected.has(option.value)}
            onChange={(event) => {
              const next = new Set(selected);
              if (event.target.checked) next.add(option.value); else next.delete(option.value);
              change(field.key, options.map((o) => o.value).filter((v) => next.has(v)));
            }} />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );
}

function OrderableList({ field, value, disabled, change }: { field: SettingsField; value: unknown; disabled: boolean; change: Change }) {
  const t = useT();
  const options = (field.options as OrderableOption[] | undefined) ?? [];
  const items = orderedItems(value, options);
  const set = (next: OrderableItem[]) => change(field.key, next);
  return (
    <ol className={styles.ordered} aria-label={field.label}>
      {items.map((item, index) => {
        const option = options.find((o) => o.id === item.id);
        return (
          <li key={item.id} className={styles.orderedItem}>
            <label className={styles.checkbox}>
              <input type="checkbox" checked={item.enabled} disabled={disabled || option?.isLocked}
                onChange={(event) => set(items.map((i) => i.id === item.id ? { ...i, enabled: event.target.checked } : i))} />
              <span>
                {option?.label ?? item.id}
                {option?.description && <small className={styles.description}>{option.description}</small>}
                {option?.disabledReason && <small className={styles.hint}>{option.disabledReason}</small>}
              </span>
            </label>
            <span className={styles.orderButtons}>
              <button type="button" className={styles.iconButton} disabled={disabled || index === 0}
                aria-label={t('Move {item} up', { item: option?.label ?? item.id })}
                onClick={() => set(moveItem(items, index, -1, options))}>
                <ArrowUp size={14} aria-hidden="true" focusable={false} />
              </button>
              <button type="button" className={styles.iconButton} disabled={disabled || index === items.length - 1}
                aria-label={t('Move {item} down', { item: option?.label ?? item.id })}
                onClick={() => set(moveItem(items, index, 1, options))}>
                <ArrowDown size={14} aria-hidden="true" focusable={false} />
              </button>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function Table({ field, value, disabled, change }: { field: SettingsField; value: unknown; disabled: boolean; change: Change }) {
  const t = useT();
  const columns: TableColumn[] = field.columns ?? [];
  const rows = Array.isArray(value) ? value.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object') : [];
  const set = (next: Record<string, unknown>[]) => change(field.key, next);
  const cell = (row: Record<string, unknown>, index: number, column: TableColumn) => {
    const update = (cellValue: unknown) => set(rows.map((r, i) => i === index ? { ...r, [column.key]: cellValue } : r));
    const label = `${column.label} ${index + 1}`;
    const current = row[column.key];
    if (column.type === 'checkbox') {
      return <input type="checkbox" aria-label={label} disabled={disabled} checked={current === true} onChange={(e) => update(e.target.checked)} />;
    }
    if (column.type === 'select') {
      return (
        <select className={styles.input} aria-label={label} disabled={disabled} value={typeof current === 'string' ? current : ''}
          onChange={(e) => update(e.target.value)}>
          <option value="">{t('Choose…')}</option>
          {(column.options ?? []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      );
    }
    if (column.type === 'multiselect') {
      return <input type="text" className={styles.input} aria-label={label} disabled={disabled}
        value={Array.isArray(current) ? current.join(', ') : ''} placeholder={column.placeholder}
        onChange={(e) => update(e.target.value.split(',').map((v) => v.trim()).filter(Boolean))} />;
    }
    return <input type={column.type === 'password' ? 'password' : 'text'} className={styles.input} aria-label={label}
      disabled={disabled} value={typeof current === 'string' ? current : ''} placeholder={column.placeholder}
      onChange={(e) => update(e.target.value)} />;
  };
  return (
    <div className={styles.table}>
      {rows.length === 0 && <p className={styles.hint}>{field.emptyMessage || t('Nothing added yet.')}</p>}
      {rows.map((row, index) => (
        <div key={index} className={styles.tableRow}>
          {columns.map((column) => (
            <label key={column.key} className={styles.tableCell}>
              <span>{column.label}</span>
              {cell(row, index, column)}
            </label>
          ))}
          <button type="button" className={styles.iconButton} disabled={disabled}
            aria-label={t('Remove row {number}', { number: index + 1 })} onClick={() => set(rows.filter((_, i) => i !== index))}>
            <Trash2 size={14} aria-hidden="true" focusable={false} />
          </button>
        </div>
      ))}
      <button type="button" className={styles.secondary} disabled={disabled} onClick={() => set([...rows, newTableRow(columns)])}>
        <Plus size={14} aria-hidden="true" focusable={false} /><span>{field.addLabel || t('Add')}</span>
      </button>
    </div>
  );
}
