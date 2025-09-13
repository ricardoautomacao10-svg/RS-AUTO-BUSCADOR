const API_BASE = "http://localhost:8000";

export const NewsArticle = {
  list: async (keyword = null, limit = 50) => {
    let url = new URL(`${API_BASE}/articles`);
    if (keyword) url.searchParams.append("keyword", keyword);
    url.searchParams.append("limit", limit);
    const res = await fetch(url);
    if (!res.ok) throw new Error("Erro ao buscar artigos");
    return res.json();
  }
};
