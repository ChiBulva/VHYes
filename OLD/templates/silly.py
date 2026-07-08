import requests

def fetch_book_by_isbn(isbn):
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json"
    response = requests.get(url)
    data = response.json()
    return data.get(f'ISBN:{isbn}', {})

isbn = "190435713X"  # Replace with the ISBN you are interested in
book_data = fetch_book_by_isbn(isbn)
print(book_data)
