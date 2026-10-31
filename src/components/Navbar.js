import React, { useContext, useState } from 'react';
import { Link } from 'react-router-dom';
import { AuthContext } from '../context/AuthContext';

const Navbar = () => {
    const { auth, logout } = useContext(AuthContext);
    const [isOpen, setIsOpen] = useState(false);

    const closeMenu = () => setIsOpen(false);

    return (
        <header className="site-nav">
            <nav className="site-nav__inner" aria-label="Main navigation">
                <Link to={auth.token ? '/dashboard' : '/'} className="site-brand" onClick={closeMenu}>
                    <img className="site-brand__mark" src="/brand-mark.svg" alt="" aria-hidden="true" />
                    <span>Resume<span className="site-brand__accent">Signal</span></span>
                </Link>
                <button
                    type="button"
                    className="site-nav__toggle"
                    aria-label={isOpen ? 'Close navigation menu' : 'Open navigation menu'}
                    aria-expanded={isOpen}
                    aria-controls="site-nav-links"
                    onClick={() => setIsOpen((open) => !open)}
                >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        {isOpen ? <path d="m6 6 12 12M18 6 6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
                    </svg>
                </button>
                <div id="site-nav-links" className={`site-nav__links${isOpen ? ' is-open' : ''}`}>
                    {auth.token ? (
                        <>
                            <Link to="/dashboard" onClick={closeMenu}>Workspace</Link>
                            <Link to="/results" onClick={closeMenu}>Results</Link>
                            <button type="button" className="site-nav__action" onClick={() => { logout(); closeMenu(); }}>
                                Sign out
                            </button>
                        </>
                    ) : (
                        <>
                            <Link to="/" onClick={closeMenu}>Sign in</Link>
                            <Link to="/signup" className="site-nav__action" onClick={closeMenu}>Create account</Link>
                        </>
                    )}
                </div>
            </nav>
        </header>
    );
};

export default Navbar;
