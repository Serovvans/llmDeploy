/** Типы контракта `docs/portal-api.md` (§1.3, §1.4, §2.3, §2.4, §3, §4). Имена полей — как в контракте. */

export type LoginStep = 'second_factor' | 'password_change' | 'second_factor_setup' | 'ready';

export type Role = 'admin' | 'employee';

export interface SessionUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  second_factor_configured: boolean;
  backup_codes: { remaining: number; total: number } | null;
}

export interface Session {
  step: LoginStep;
  user: SessionUser | null;
}

export interface SecondFactorResult {
  session: Session;
  backup_code_used: boolean;
}

export interface SecondFactorSetup {
  secret: string;
  qr: { size: number; path: string };
}

export interface SecondFactorConfirmResult {
  session: Session;
  backup_codes: string[];
}

export type AdminUserState = 'active' | 'never_logged_in' | 'blocked';

export interface AdminUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  state: AdminUserState;
  second_factor_configured: boolean;
  is_me: boolean;
  created_at: string;
}

export interface TemporaryPasswordResult {
  user: AdminUser;
  temporary_password: string;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export type SortOrder = 'asc' | 'desc';

export interface PortalConfig {
  password: { min_length: number; max_length: number };
  dialogs: { message_max_chars: number };
  chat: {
    attachment_max_bytes: number;
    attachment_max_pages: number;
    attachment_extensions: string[];
    max_attachments: number;
    max_images: number;
  };
  kb: { document_max_bytes: number; document_max_pages: number; document_extensions: string[] };
  docparse: {
    document_max_bytes: number;
    max_pages: number;
    document_extensions: string[];
    templates: { id: string; title: string; description: string; free_form: boolean }[];
  };
  sql: { dialects: { id: string; title: string }[]; default_dialect: string; schema_max_chars: number };
}

export interface FieldError {
  field: string;
  code: string;
  message: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  fields?: FieldError[];
  details?: Record<string, unknown>;
}
