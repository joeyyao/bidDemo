import axios from 'axios'
const api = axios.create({ baseURL: 'http://localhost:8000/api' })
api.interceptors.request.use(config => { const token = localStorage.getItem('contract_token'); if (token) config.headers.Authorization = `Bearer ${token}`; return config })
api.interceptors.response.use(r => r, e => { const message = e.response?.data?.detail || '网络连接失败，请检查服务是否启动'; return Promise.reject(new Error(message)) })
export default api
