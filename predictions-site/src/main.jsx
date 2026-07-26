import React from 'react';
import { createRoot } from 'react-dom/client';
import Ferrofluid from './Ferrofluid';
import './landing.css';

createRoot(document.getElementById('ferrofluid-root')).render(<Ferrofluid colors={['#ffffff','#ffffff','#ffffff']} speed={0.5} scale={1.6} turbulence={1} fluidity={0.1} rimWidth={0.2} sharpness={2.5} shimmer={1.5} glow={2} flowDirection="down" opacity={1} mouseInteraction mouseStrength={1} mouseRadius={0.35} />);
