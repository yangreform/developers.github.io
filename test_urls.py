from googlesearch import search
query = 'site:propertyguru.com.sg/project "THREE BALMORAL"'
urls = list(search(query, num_results=5, lang='en'))
print('URLs:', urls)
