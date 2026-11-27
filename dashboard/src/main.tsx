import { createRoot } from 'react-dom/client'
import 'pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
import App from './App'

const root = document.getElementById('root')
if (root) createRoot(root).render(<App />)
