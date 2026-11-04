"""TF-IDF Vectorizer using numpy."""
import re
import numpy as np

ENGLISH_STOP_WORDS = frozenset({
    "a", "about", "above", "across", "after", "afterwards", "again", "against",
    "all", "almost", "alone", "along", "already", "also", "although", "always",
    "am", "among", "amongst", "amoungst", "amount", "an", "and", "another",
    "any", "anyhow", "anyone", "anything", "anyway", "anywhere", "are",
    "around", "as", "at", "back", "be", "became", "because", "become",
    "becomes", "becoming", "been", "before", "beforehand", "behind", "being",
    "below", "beside", "besides", "between", "beyond", "bill", "both",
    "bottom", "but", "by", "call", "can", "cannot", "cant", "co", "con",
    "could", "couldnt", "cry", "de", "describe", "detail", "do", "done",
    "down", "due", "during", "each", "eg", "eight", "either", "eleven", "else",
    "elsewhere", "empty", "enough", "etc", "even", "ever", "every", "everyone",
    "everything", "everywhere", "except", "few", "fifteen", "fifty", "fill",
    "find", "fire", "first", "five", "for", "former", "formerly", "forty",
    "found", "four", "from", "front", "full", "further", "get", "give", "go",
    "had", "has", "hasnt", "have", "he", "hence", "her", "here", "hereafter",
    "hereby", "herein", "hereupon", "hers", "herself", "him", "himself", "his",
    "how", "however", "hundred", "i", "ie", "if", "in", "inc", "indeed",
    "interest", "into", "is", "it", "its", "itself", "keep", "last", "latter",
    "latterly", "least", "less", "ltd", "made", "many", "may", "me",
    "meanwhile", "might", "mill", "mine", "more", "moreover", "most", "mostly",
    "move", "much", "must", "my", "myself", "name", "namely", "neither",
    "never", "nevertheless", "next", "nine", "no", "nobody", "none", "noone",
    "nor", "not", "nothing", "now", "nowhere", "of", "off", "often", "on",
    "once", "one", "only", "onto", "or", "other", "others", "otherwise", "our",
    "ours", "ourselves", "out", "over", "own", "part", "per", "perhaps",
    "please", "put", "rather", "re", "same", "see", "seem", "seemed",
    "seeming", "seems", "serious", "several", "she", "should", "show", "side",
    "since", "sincere", "six", "sixty", "so", "some", "somehow", "someone",
    "something", "sometime", "sometimes", "somewhere", "still", "such",
    "system", "take", "ten", "than", "that", "the", "their", "them",
    "themselves", "then", "thence", "there", "thereafter", "thereby",
    "therefore", "therein", "thereupon", "these", "they", "thick", "thin",
    "third", "this", "those", "though", "three", "through", "throughout",
    "thru", "thus", "to", "together", "too", "top", "toward", "towards",
    "twelve", "twenty", "two", "un", "under", "until", "up", "upon", "us",
    "very", "via", "was", "we", "well", "were", "what", "whatever", "when",
    "whence", "whenever", "where", "whereafter", "whereas", "whereby",
    "wherein", "whereupon", "wherever", "whether", "which", "while", "whither",
    "who", "whoever", "whole", "whom", "whose", "why", "will", "with",
    "within", "without", "would", "yet", "you", "your", "yours", "yourself",
    "yourselves"
})

_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")


class TfidfVectorizer:
    def __init__(self, stop_words=None):
        if stop_words == "english":
            self.stop_words = ENGLISH_STOP_WORDS
        elif isinstance(stop_words, (list, set, frozenset)):
            self.stop_words = frozenset(stop_words)
        else:
            self.stop_words = frozenset()
        self.vocabulary_ = {}
        self.idf_ = None

    def _tokenize(self, text: str):
        tokens = _TOKEN_RE.findall(text.lower())
        if self.stop_words:
            tokens = [t for t in tokens if t not in self.stop_words]
        return tokens

    def fit_transform(self, raw_documents):
        docs_tokens = [self._tokenize(d) for d in raw_documents]
        vocab = {}
        for tokens in docs_tokens:
            for t in tokens:
                if t not in vocab:
                    vocab[t] = len(vocab)
        self.vocabulary_ = vocab
        n_docs = len(raw_documents)
        n_features = len(vocab)
        if n_features == 0:
            return np.zeros((n_docs, 1), dtype=np.float64)

        tf = np.zeros((n_docs, n_features), dtype=np.float64)
        df = np.zeros(n_features, dtype=np.float64)
        for i, tokens in enumerate(docs_tokens):
            seen = set()
            for t in tokens:
                idx = vocab[t]
                tf[i, idx] += 1.0
                seen.add(idx)
            for idx in seen:
                df[idx] += 1.0

        idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0
        self.idf_ = idf
        tfidf = tf * idf
        norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        tfidf = tfidf / norms
        return tfidf
