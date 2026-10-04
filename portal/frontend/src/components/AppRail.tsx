import { IconBooksLibraryRegular24 } from '@skbkontur/icons/IconBooksLibraryRegular24';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconCommentRectTextRegular24 } from '@skbkontur/icons/IconCommentRectTextRegular24';
import { IconDocTextRegular24 } from '@skbkontur/icons/IconDocTextRegular24';
import { IconLocationMapRegular24 } from '@skbkontur/icons/IconLocationMapRegular24';
import { IconPeople2Regular24 } from '@skbkontur/icons/IconPeople2Regular24';
import { IconTechServerRegular24 } from '@skbkontur/icons/IconTechServerRegular24';
import { IconWeatherSunMoonRegular24 } from '@skbkontur/icons/IconWeatherSunMoonRegular24';
import { DropdownMenu, MenuHeader, MenuItem, MenuSeparator, SingleToast } from '@skbkontur/react-ui';
import { NavLink, useNavigate } from 'react-router-dom';

import type { SessionUser } from '../api/types';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import type { ThemePreference } from '../theme/preference';
import { useThemeChoice } from '../theme/ThemeProvider';
import styles from './AppRail.module.css';

const SECTIONS = [
  { to: '/chat', label: texts.nav.chat, Icon: IconCommentRectTextRegular24 },
  { to: '/knowledge', label: texts.nav.knowledge, Icon: IconBooksLibraryRegular24 },
  { to: '/sql', label: texts.nav.sql, Icon: IconTechServerRegular24 },
  { to: '/cogis', label: texts.nav.cogis, Icon: IconLocationMapRegular24 },
  { to: '/documents', label: texts.nav.documents, Icon: IconDocTextRegular24 },
];

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: 'system', label: texts.nav.themeSystem },
  { value: 'light', label: texts.nav.themeLight },
  { value: 'dark', label: texts.nav.themeDark },
];

const MENU_POSITIONS = ['right bottom' as const, 'right top' as const];

function itemClass({ isActive }: { isActive: boolean }): string {
  return isActive ? `${styles.item} ${styles.current}` : (styles.item ?? '');
}

function initials(fullName: string): string {
  return fullName
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join('');
}

/** Рейка разделов (концепция §3.2): пять разделов, внизу — «Пользователи», «Тема», «Профиль». */
export function AppRail({ user }: { user: SessionUser }) {
  const { preference, setPreference } = useThemeChoice();
  const { logout } = useSession();
  const navigate = useNavigate();

  const onLogout = () => {
    logout().catch((error: unknown) => SingleToast.push(errorText(error), { use: 'error' }));
  };

  return (
    <nav className={styles.rail} aria-label={texts.nav.label}>
      <div className={styles.group}>
        {SECTIONS.map(({ to, label, Icon }) => (
          <NavLink key={to} to={to} className={itemClass}>
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </NavLink>
        ))}
      </div>
      <div className={styles.group}>
        {user.role === 'admin' && (
          <NavLink to="/admin/users" className={itemClass}>
            <IconPeople2Regular24 aria-hidden="true" />
            <span>{texts.nav.users}</span>
          </NavLink>
        )}
        <DropdownMenu
          positions={MENU_POSITIONS}
          caption={
            <button type="button" className={styles.item}>
              <IconWeatherSunMoonRegular24 aria-hidden="true" />
              <span>{texts.nav.theme}</span>
            </button>
          }
        >
          {THEMES.map(({ value, label }) => (
            <MenuItem
              key={value}
              icon={value === preference ? <IconCheckARegular16 /> : undefined}
              onClick={() => setPreference(value)}
            >
              {label}
            </MenuItem>
          ))}
        </DropdownMenu>
        <DropdownMenu
          positions={MENU_POSITIONS}
          caption={
            <button type="button" className={styles.item}>
              <span className={styles.initials} aria-hidden="true">
                {initials(user.full_name)}
              </span>
              <span>{texts.nav.profile}</span>
            </button>
          }
        >
          <MenuHeader>{user.full_name}</MenuHeader>
          <MenuItem onClick={() => navigate('/profile')}>{texts.nav.profile}</MenuItem>
          <MenuSeparator />
          <MenuItem onClick={onLogout}>{texts.nav.logout}</MenuItem>
        </DropdownMenu>
      </div>
    </nav>
  );
}
