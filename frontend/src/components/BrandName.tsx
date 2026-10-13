// #609: the brand honors the admin-configured instance title everywhere the
// classic UI does (navbar + login card). A custom title renders as plain text;
// the stock name keeps the two-tone accent treatment.
export const DEFAULT_INSTANCE_NAME = 'Calibre-Web NextGen';
/** What this build calls itself. The server's stock title stays the upstream one
 *  (so existing installs and settings are untouched); it is shown as this. */
export const PRODUCT_NAME = 'Calibre-Web NextGen+';

/** The name to display: an administrator's own title as written, the product
 *  name otherwise. */
export function displayInstanceName(name?: string): string {
  return name && name !== DEFAULT_INSTANCE_NAME && name !== PRODUCT_NAME ? name : PRODUCT_NAME;
}

interface BrandNameProps {
  /** Server-provided instance_name (from /auth/config or /auth/me). */
  name?: string;
  accentClassName?: string;
}

export function BrandName({ name, accentClassName }: BrandNameProps) {
  if (displayInstanceName(name) !== PRODUCT_NAME) {
    return <>{name}</>;
  }
  return (
    <>
      Calibre-Web <span className={accentClassName}>NextGen+</span>
    </>
  );
}
