import re
import os
import json
from urllib.parse import urlparse, urljoin, urldefrag
from bs4 import BeautifulSoup
from collections import defaultdict
import threading
import hashlib

ALLOWED_BASE_DOMAINS = {
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
}

stats_lock = threading.Lock()


stats = {
    "unique_urls": set(),
    "longest_page": {"url": "", "word_count": 0},
    "word_freq": defaultdict(int),
    "subdomains": defaultdict(set),
    "simhashes": {}, 
    "near_duplicates": []
}

SIMHASH_BITS = 64   
SIMILARITY_THRESHOLD = 0.9

last_saved_count = 0
SAVE_INTERVAL = 200


STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", 
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been", 
    "before", "being", "below", "between", "both", "but", "by", "can't", 
    "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", 
    "doing", "don't", "down", "during", "each", "few", "for", "from", 
    "further", "had", "hadn't", "has", "hasn't", "have", "haven't", "having", 
    "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", 
    "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", 
    "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "let's", 
    "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", 
    "off", "on", "once", "only", "or", "other", "ought", "our", "ours", 
    "ourselves", "out", "over", "own", "same", "shan't", "she", "she'd", 
    "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", 
    "that", "that's", "the", "their", "theirs", "them", "themselves", "then", 
    "there", "there's", "these", "they", "they'd", "they'll", "they're", 
    "they've", "this", "those", "through", "to", "too", "under", "until", 
    "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", 
    "were", "weren't", "what", "what's", "when", "when's", "where", "where's", 
    "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't", 
    "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", 
    "yours", "yourself", "yourselves"
}

STATS_FILE = "crawler_stats.json"


def save_stats():
    """
    Saving the collected crawler statistics to JSON file (crawler_stats.json)
    
    Saved data includes:
    unique_urls (list[str]) : List of all unique, normalized, valid URLs
    longest_page (dict) : the information about the longest page
    word_freq (dict): frequency count of words across processed pages
    subdomains (dict[str, list[str]]): a list of unique URL paths under each subdomain
    
    Args:
        None
    Returns:
        None
    """
    with stats_lock:
        data = {
            "unique_urls": list(stats["unique_urls"]),
            "longest_page": stats["longest_page"],
            "word_freq": dict(stats["word_freq"]),
            "subdomains": {k: list(v) for k, v in stats["subdomains"].items()},
            "simhashes": stats["simhashes"],
            "near_duplicates": stats["near_duplicates"],
        }
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] save_stats: {e}")


def load_stats():
    """
    Load previously saved crawler statistics from JSON file to memory.
    After loading, last_saved_count is updated to reflect the number of unique URLs already processed. 
    
    Args:
        None
        
    Return:
        None
    """
    global stats, last_saved_count
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with stats_lock:
                stats["unique_urls"] = set(data.get("unique_urls", []))
                stats["longest_page"] = data.get("longest_page", {"url": "", "word_count": 0})
                stats["word_freq"] = defaultdict(int, data.get("word_freq", {}))
                stats["subdomains"] = defaultdict(set,
                    {k: set(v) for k, v in data.get("subdomains", {}).items()})
                stats["simhashes"] = data.get("simhashes", {})
                stats["near_duplicates"] = data.get("near_duplicates", [])

                last_saved_count = len(stats["unique_urls"])
            print(f"[INFO] Loaded {len(stats['unique_urls'])} URLs")
        except Exception as e:
            print(f"[WARN] load_stats: {e}")

load_stats()


def scraper(url, resp):
    """
    Scraping a list of links from the page

    Args:
        url (str): the url that was used to get the current url page
        resp (str): the current actual url page that is scraped
    Returns:
        list: a list of valid links on the page
    """
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    """
    Process a fetched page and extract valid outgoing hyperlink on the page.

    Performs serveral validation and filtering steps before processing a page:
    - Ensures the HTTP response is successful (status 200)
    - Skips empty or large file pages (>5MB)
    - Processes only HTML content page
    - Removes non-visible elements (script, style, noscript)
    - Filters out non-web hyperlinks on the page
    - Removes URL fragments from the current fetched page URL and the hyperlinks on the page.
    
    After validating, extract the current page URL, page content, and word count and save in the crawler_stats.json file
    The valid hyperlinks are added to the list and the list of links is returned
    
    Args:
        url (str): url: the URL that was used to get the page
        resp (str): The response object returned by the crawler framework
        resp.url: the actual url of the page
        resp.status: the status code returned by the server. 200 is OK, you got the page. Other numbers mean that there was some kind of problem.
        resp.error: when status is not 200, you can check the error here, if needed.
        resp.raw_response: this is where the page actually is. More specifically, the raw_response has two parts:
        resp.raw_response.url: the url, again
        resp.raw_response.content: the content of the page!

    Returns:
        list[str]: 
        A list of extracted links on the current URL page
        Returns an empty list if the page is invalid, too large, non-HTML, contains no text, or cannot be parsed.
    """
    #basic check
    if resp is None or resp.status != 200 or resp.raw_response is None:
        return []
    
    try:
        content = resp.raw_response.content
    except Exception:
        return []
    
    if not content or len(content) == 0:
        return []
    
    #for big docum
    if len(content) > 5 * 1024 * 1024:
        return []
    
    #type check
    try:
        content_type = resp.raw_response.headers.get('Content-Type', '')
        if content_type and 'text/html' not in content_type.lower():
            return []
    except Exception:
        pass

    #parse HTML
    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception:
            return []
        
    base_url = url
    if hasattr(resp.raw_response, 'url') and resp.raw_response.url:
        base_url = resp.raw_response.url

    clean_url, _ = urldefrag(base_url)

    #Check if clean_url is still within the allowed domain.
    parsed_clean = urlparse(clean_url)
    if not is_allowed_host((parsed_clean.hostname or "").lower()):
        return []
    
    #Extract text
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()

    text = soup.get_text(separator=' ', strip=True)
    words = tokenize(text)
    word_count = len(words)

    #statistic
    collect_statistics(clean_url, words, word_count)

    #Extract links
    out_links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith(("mailto:", "javascript:", "tel:", "ftp:", "file:", "#")):
            continue
        try:
            abs_url = urljoin(base_url, href)
            abs_url, _ = urldefrag(abs_url)
            if abs_url:
                out_links.append(abs_url)
        except ValueError:
            continue

    return out_links

def compute_simhash(words):
    v = [0] * SIMHASH_BITS
    for word in words:
        word_hash = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16)
        for i in range(SIMHASH_BITS):
            bit = (word_hash >> i) & 1
            if bit:
                v[i] += 1
            else:
                v[i] -= 1
    
    fingerprint = 0
    for i in range(SIMHASH_BITS):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint

def simhash_similarity(hash1, hash2):
    xor = hash1 ^ hash2
    distance = bin(xor).count('1')
    return 1 - (distance / SIMHASH_BITS)

def collect_statistics(url, words, word_count):
    """
    Saving the web page URL name and statistics to global stats dictionary and JSON file 
    Before saving the data to JSON file,
    - Ensures each URL is only processed once (uniqueness tracking)
    - Records subdomain and its corresponding valid subdomain paths
    - Updates longest page record 
    - Update word frequency statistics
    
    Args:
        url (str): the fetched page URL name 
        words (str): tokenized visible words extracted from the page 
        word_count (int): number of tokens/words in that page.
        
    Returns:
        None
    """
    global last_saved_count

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    page_simhash = None
    if word_count >= 50:  # only for long page
        page_simhash = compute_simhash(words)

    with stats_lock:
        if url in stats["unique_urls"]:
            return

        # Q1
        stats["unique_urls"].add(url)

        # Q4
        if hostname and (hostname == "uci.edu" or hostname.endswith(".uci.edu")):
            stats["subdomains"][hostname].add(url)

        is_dup = False
        if page_simhash is not None:
            for existing_url, existing_hash in stats["simhashes"].items():
                similarity = simhash_similarity(page_simhash, existing_hash)
                if similarity >= SIMILARITY_THRESHOLD:
                    # find repeat
                    stats["near_duplicates"].append({
                        "url1": url,
                        "url2": existing_url,
                        "similarity": similarity
                    })
                    is_dup = True
                    break # find one enough
            
            if not is_dup:
                stats["simhashes"][url] = page_simhash

        # Q2&Q3
        if not is_dup and word_count >= 50:
            if word_count > stats["longest_page"]["word_count"]:
                stats["longest_page"]["url"] = url
                stats["longest_page"]["word_count"] = word_count

            for w in words:
                if w not in STOP_WORDS and len(w) >= 2:
                    stats["word_freq"][w] += 1

        current_count = len(stats["unique_urls"])

    if current_count - last_saved_count >= SAVE_INTERVAL:
        save_stats()
        last_saved_count = current_count
        # show bonus
        dup_count = len(stats.get('near_duplicates', []))
        print(f"[STATS] Unique: {current_count}, Subdomains: {len(stats['subdomains'])}, Near-Dups: {dup_count}")

def tokenize(text):
    """
    convert the text with a length greater than 2 to lower case and tokenize

    Args:
        text (str): the text on the page

    Returns:
        list[str]: a list of normalized word tokens
    """
    return re.findall(r'\b[a-zA-Z]{2,}\b', text.lower())

def is_allowed_host(host):
    """
    Checking if the URL hostname is within allowed domain

    Args:
        host (str): URL hostname

    Returns:
        bool:
            if host is empty, return False
            if host is within allowed domain, return True
    """
    if not host:
        return False
    host = host.lower()
    for base in ALLOWED_BASE_DOMAINS:
        if host == base or host.endswith("." + base):
            return True
    return False


def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    
    """
    Determines whether a URL should be crawled. 
    Added multiple filtering rules to avoid crawler traps.
    
    Some Crawler trap detections:
    Avoids infinite or dynamically generated URL spaces, including:
        - Calendar and event pages
        - Query parameters indicating pagination, sorting, filtering, sessions, or dynamic states
        - Dynamic web pages
        - long URLs (length > 500 characters)
        - Versioning or history parameters
        - excessively deep paths (e.g. repeated loops of the url)
        - large dataset files, ML dataset storage like UCI ML dataset

    Args:
        url (str): the URL name
    Returns:
        bool: 
            return True if the 
    """
    try:
        parsed = urlparse(url)

        if parsed.scheme not in set(["http", "https"]):
            return False
        
        host = (parsed.hostname or "").lower()
        if not is_allowed_host(host):
            return False
        
        path = parsed.path.lower()
        query = parsed.query.lower()

        if re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            + r"|lif|ff|txt)$", parsed.path.lower()): #I believe lif and ff is useless for this assignment
                return False
        
        if '/calendar' in path:
            return False
        
        if 'ical' in query:
            return False

        if '/events/' in path:
            return False
        
        if re.search(r'[?&](date|day|month|year)=', query):
            return False

        match = re.search(r'[?&]page=(\d+)', query)
        if match and int(match.group(1)) > 10:
            return False

        match = re.search(r'[?&]offset=(\d+)', query)
        if match and int(match.group(1)) > 500:
            return False

        if re.search(r'[?&](sort|order|filter)=', query) and query.count('&') >= 4:
            return False

        if re.search(r'[?&](action|do)=(edit|history|login|delete)', query):
            return False

        if re.search(r'[?&](diff|oldid|version)=', query):
            return False

        if '/wp-admin/' in path or '/wp-json/' in path:
            return False

        if '/feed' in path or '/rss' in path:
            return False

        if re.search(r'[?&](session|sid|jsession|phpsessid)=', query):
            return False

        if len(url) > 500:
            return False

        segments = [s for s in path.split('/') if s]
        if len(segments) > 8:
            return False

        if len(segments) >= 3:
            seen = {}
            for seg in segments:
                seen[seg] = seen.get(seg, 0) + 1
                if seen[seg] > 2:
                    return False

        if 'doku.php' in path:
            return False

        if re.search(r'[?&]do=', query):
            return False

        if '/datasets/' in path:
            return False

        if '/data/' in path and '/class/' in path:
            return False

        if re.search(r'[?&]version=', query):
            return False

        if '/timeline' in path:
            return False

        if '/raw-attachment/' in path or '/zip-attachment/' in path:
            return False

        if re.search(r'[?&]format=', query):
            return False

        if re.search(r'[?&](version|precision|diff)=', query):
            return False

        if 'archive.ics.uci.edu' in url and '/ml/' in path:
            return False

        return True

    except TypeError:
        print ("TypeError for ", parsed)
        raise
    except Exception:
        return False
    

def generate_report():
    """
    Reporting the statistics of the data
    Args:
        None
        
    Return:
        None
    """
    load_stats()

    lines = []
    lines.append("=" * 70)
    lines.append("WEB CRAWLER REPORT")
    lines.append("=" * 70)

    lines.append(f"\nQ1. Unique pages: {len(stats['unique_urls'])}")

    lines.append(f"\nQ2. Longest page:")
    lines.append(f"    URL: {stats['longest_page']['url']}")
    lines.append(f"    Words: {stats['longest_page']['word_count']}")

    lines.append(f"\nQ3. Top 50 words:")
    sorted_words = sorted(stats['word_freq'].items(), key=lambda x: (-x[1], x[0]))[:50]
    for i, (word, count) in enumerate(sorted_words, 1):
        lines.append(f"    {i:2d}. {word}: {count}")

    lines.append(f"\nQ4. Subdomains ({len(stats['subdomains'])} total):")
    for subdomain in sorted(stats['subdomains'].keys()):
        lines.append(f"    {subdomain}, {len(stats['subdomains'][subdomain])}")

    dup_count = len(stats.get('near_duplicates', []))
    lines.append(f"\n Near-duplicates detected: {dup_count}")

    #if dup_count > 0:
    #   lines.append("Examples:")
    #    for dup in stats['near_duplicates'][:5]:
    #        lines.append(f"    {dup['url1']} ~= {dup['url2']} ({dup['similarity']:.2%})")

    lines.append("\n" + "=" * 70)

    report = '\n'.join(lines)
    print(report)

    with open("report.txt", 'w', encoding='utf-8') as f:
        f.write(report)
    print("\n[INFO] Report saved to report.txt")

    save_stats()

if __name__ == "__main__":
    generate_report()