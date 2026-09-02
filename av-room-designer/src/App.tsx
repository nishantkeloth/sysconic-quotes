import { useEffect, useState } from 'react';
import { getToken, logout } from './api/client';
import LoginPage from './pages/LoginPage';
import ProjectsPage from './pages/ProjectsPage';
import RoomDesignerPage from './pages/RoomDesignerPage';

// Top-level view state machine, deliberately simple (no router library) --
// the app only ever has three screens deep: projects list -> a project's
// rooms -> a single room's 2D designer. Selection is tracked by id only;
// each page fetches its own data on mount so navigating back always shows
// fresh state rather than stale cached objects.
type View =
  | { name: 'projects' }
  | { name: 'room-designer'; projectId: string; roomId: string };

export default function App() {
  const [authed, setAuthed] = useState<boolean>(!!getToken());
  const [view, setView] = useState<View>({ name: 'projects' });

  useEffect(() => {
    if (!authed) setView({ name: 'projects' });
  }, [authed]);

  if (!authed) {
    return <LoginPage onLoggedIn={() => setAuthed(true)} />;
  }

  return (
    <div className="avrd-app">
      <div className="avrd-topbar">
        <div className="avrd-topbar-title">AV Room Designer</div>
        <div className="avrd-topbar-actions">
          <button
            className="avrd-btn"
            onClick={() => {
              logout();
              setAuthed(false);
            }}
          >
            Log out
          </button>
        </div>
      </div>

      {view.name === 'projects' && (
        <ProjectsPage
          onOpenRoom={(projectId, roomId) => setView({ name: 'room-designer', projectId, roomId })}
        />
      )}

      {view.name === 'room-designer' && (
        <RoomDesignerPage
          projectId={view.projectId}
          roomId={view.roomId}
          onBack={() => setView({ name: 'projects' })}
        />
      )}
    </div>
  );
}
