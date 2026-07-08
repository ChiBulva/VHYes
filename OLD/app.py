from flask import Flask, request, jsonify, render_template
from imdb import IMDb
import csv 
import ast  # Abstract Syntax Trees for safely evaluating strings into data structures

import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
ia = IMDb()

import json

# Function to remove duplicates from a list while preserving order
def remove_duplicates(input_list):
    seen = set()
    output_list = []
    for item in input_list:
        if item not in seen:
            output_list.append(item)
            seen.add(item)
    return output_list

# Load the JSON data from the file
file_path = './assets/JSON/Catalog.json'

with open(file_path, 'r') as json_file:
    try:
        data = json.load(json_file)
    except json.JSONDecodeError:
        print('Error: Unable to parse JSON data in the file.')
        exit(1)

# Check if 'cast' key exists and is a list in the JSON data
if 'cast' in data and isinstance(data['cast'], list):
    data['cast'] = remove_duplicates(data['cast'])

# Write the deduplicated data back to the file
with open(file_path, 'w') as json_file:
    json.dump(data, json_file, indent=4)

print('Duplicates removed and data saved to Catalog.json.')
def extract_unique_items(csv_file, column_name):
    unique_items = set()
    
    with open(csv_file, mode='r', encoding='utf-8') as file:
        csv_reader = csv.DictReader(file)
        
        for row in csv_reader:
            items = row[column_name].strip('[]').replace("'", "").split(', ')
            unique_items.update(items)
    
    return list(unique_items)

# Example usage to extract unique genres
#unique_genres = extract_unique_items('./assets/CSV/Catalog.csv', 'rating')
#print(unique_genres)

def apply_filters(movie_list, search_filter, genre_filter, rating_filter):
    filtered_movies = []

    for movie in movie_list:
        title = str(movie['title']).lower()
        cast = str(movie['cast']).lower()
        plot_outline = str(movie['plot_outline']).lower()
        genres = str(movie['genres']).lower()
        rating = float(movie['rating'])

        # Apply filters
        title_match = not search_filter or search_filter.lower() in title
        cast_match = not search_filter or search_filter.lower() in cast
        plot_match = not search_filter or search_filter.lower() in plot_outline
        genre_match = not genre_filter or genre_filter.lower() in genres
        rating_match = not rating_filter or rating >= float(rating_filter)

        if title_match or cast_match or plot_match:
            if genre_match and rating_match:
                filtered_movies.append(movie)

    return filtered_movies

@app.route('/print/<key>', methods=['GET'])
def print_keys(key):
    try:
        # Open and read the CSV file
        with open('./assets/CSV/Catalog.CSV', 'r', encoding='utf-8') as csv_file:
            csv_reader = csv.DictReader(csv_file)

            # Extract keys from the CSV data
            keys = [row[key] for row in csv_reader]

            # Sort the keys alphabetically
            sorted_keys = sorted(keys)

        # Render an HTML template with the sorted keys
        return render_template('sorted_keys.html', keys=sorted_keys)

    except FileNotFoundError:
        return "CSV file not found."
    except Exception as e:
        return str(e)

@app.route('/get_movies_filtered', methods=['POST'])
def get_movies_filtered():
    # Read the filter criteria from the request
    search_filter = request.form.get('title', '')
    genre_filter = request.form.get('genre', '')
    rating_filter = request.form.get('rating', '')
    yearFilterStart = request.form.get('yearFilterStart', '1800')  # Add this line
    yearFilterEnd = request.form.get('yearFilterEnd', '18000')  # Add this line

    movie_data = []

    with open('./assets/CSV/Catalog.csv', mode='r', encoding='utf-8') as file:
        csv_reader = csv.DictReader(file)

        for row in csv_reader:
            # Set a default rating for movies without a rating
            rating = float(row['rating']) if row['rating'] != 'No rating available.' else 5

            movie = {
                'title': row['title'],
                'year': row['year'],
                'director': row['director'].strip('[]').replace("'", "").split(', '),
                'rating': rating,
                'genres': row['genres'].strip('[]').replace("'", "").split(', '),
                'image': row['image'],
                'cast': row['cast'].strip('[]').replace("'", "").split(', '),
                'plot_outline': row['plot outline'],
                'votes': row['votes'],
                'id': row['id']
            }

            if (
                (not search_filter or 
                search_filter.lower() in str(movie['title']).lower() or
                search_filter.lower() in str(movie['cast']).lower() or
                search_filter.lower() in str(movie['plot_outline']).lower())
                and
                (not genre_filter or genre_filter.lower() in str(movie['genres']).lower())
                and
                (not rating_filter or float(rating) >= float(rating_filter))
                and
                (not yearFilterStart or int(movie['year']) >= int(yearFilterStart))  # Added year filtering
                and
                (not yearFilterEnd or int(movie['year']) <= int(yearFilterEnd))  # Added year filtering
            ):
                movie_data.append(movie)

    return jsonify({'movies': movie_data})


@app.route('/get_movies', methods=['POST'])
def get_movies():
    movie_data = []
    
    with open('./assets/CSV/Catalog.csv', mode ='r', encoding='utf-8') as file:
        csv_reader = csv.DictReader(file)
        
        for row in csv_reader:
            movie = {
                'title': row['title'],
                'year': row['year'],
                'director': row['director'].strip('[]').replace("'", "").split(', '),
                'rating': row['rating'],
                'genres': row['genres'].strip('[]').replace("'", "").split(', '),
                'image': row['image'],
                'cast': row['cast'].strip('[]').replace("'", "").split(', '),
                'plot_outline': row['plot outline'],
                'votes': row['votes'],
            }
            movie_data.append(movie)

    return jsonify({'movies': movie_data})

@app.route('/search', methods=['POST'])
def search_movie():
    query = request.json.get('query', "")

    movies = ia.search_movie(query)
    #print(movies)
    results = []
    for movie in movies:
        # Use get method to avoid KeyError
        movie_id = movie.getID()
        title = movie.get('title', "Unknown Title")
        year = movie.get('year', "Unknown Year")
        cover_url = movie.get('cover url', "Unknown URL")

        results.append({
            "id": movie_id,
            "title": title,
            "year": year,
            "cover_url": cover_url
        })
    return jsonify(results)

@app.route('/details', methods=['POST'])
def movie_details():
    movie_id = request.json.get('id', "")
    movie_data = request.json
    #print(movie_data)
    movie = ia.get_movie(movie_id)
    
    details = {
        "title": movie.get('title', "Unknown Title"),
        "year": movie.get('year', "Unknown Year"),
        "image": movie.get('full-size cover url', "Path/to/default/image"),
        "genres": movie.get('genres', []),
        "plot outline": movie.get('plot outline', "No outline available."),
        "director": [person['name'] for person in movie.get('director', [])],
        "cast": [person['name'] for person in movie.get('cast', [])[:4]],  # Top 4 actors
        "runtime": movie.get('runtime', []),
        "rating": movie.get('rating', "No rating available."),
        "votes": movie.get('votes', "No votes available."),
        "certificates": movie.get('certificates', [])
    }
    
    return jsonify(details)



import json
from json.decoder import JSONDecodeError

def transform_movie_data(data):
    # Extract the relevant information from the provided data
    cast = data[0]
    certificates = data[1]
    director = data[2]
    genres = data[3]
    image = data[4]
    plot_outline = data[5]
    rating = data[6]
    runtime = data[7]
    title = data[8]
    votes = data[9]
    year = data[10]
    movie_id = data[11]

    # Create a dictionary in the desired format
    movie_dict = {
        'id': movie_id,
        'title': title,
        'year': year,
        'director': director,
        'rating': rating,
        'genres': genres,
        'image': image,
        'cast': cast,
        'plot outline': plot_outline,
        'votes': votes
    }

    return movie_dict

def string_to_list(string_names):
    # Split the input string by comma and strip whitespace
    names = [name.strip() for name in string_names.split(',')]
    # Convert the list of names to a formatted string
    formatted_names = str(names)
    return formatted_names

@app.route('/fast-add-by-id', methods=['POST'])
def fast_add_movie_by_id():
    
    
    csv_file_path = './assets/CSV/Catalog.csv'
    
    data = request.get_json()
    imdb_id = data.get('id')
        
    print(imdb_id)
    # Use IMDbPY to fetch movie details using IMDb ID
    movie = ia.get_movie(str(imdb_id))

    # Extract relevant information from the movie object
    title = movie.get('title', 'N/A')
    # Extract movie details from the IMDbPY movie object
    year = movie.get('year', 'N/A')
    director = [str(director) for director in movie.get('director', ['N/A'])]
    rating = movie.get('rating', 'N/A')
    genres = [str(genre) for genre in movie.get('genres', ['N/A'])]
    image_url = movie.get('cover url', 'N/A')
    cast = [str(actor) for actor in movie.get('cast', ['N/A'])]
    plot_outline = movie.get('plot outline', 'N/A')
    votes = movie.get('votes', 'N/A')

    # Extract additional details from the movie object
    certificates = [str(cert) for cert in movie.get('certificates', ['N/A'])]
    runtime = movie.get('runtimes', ['N/A'])[0] if 'runtimes' in movie else 'N/A'

    # Load existing movies
    existing_movies = []
    try:
        with open(csv_file_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_movies.append(row)
    except FileNotFoundError:
        pass

    # Calculate the next movie ID based on existing data
    movie_id = calculate_next_movie_id(existing_movies)

    # Open the CSV file in append mode and write the new movie with ID
    with open(csv_file_path, mode='a', newline='\n', encoding='utf-8') as file:
        fieldnames = ['cast', 'certificates', 'director', 'genres', 'image', 'plot outline', 'rating', 'runtime', 'title', 'votes', 'year', 'id']
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        
        cast = str(cast)
        certificates = str(certificates)
        director = str(director)
        genres = str(genres)

        # Write the movie data to the CSV file, including the generated ID
        writer.writerow({
            'cast': cast,
            'certificates': certificates,
            'director': director,
            'genres': genres,
            'image': image_url,
            'plot outline': plot_outline,
            'rating': rating,
            'runtime': runtime,
            'title': title,
            'votes': votes,
            'year': year,
            'id': movie_id,
        })
        print("Successfully Fast Added:\t" + str(title))


    return jsonify({'status': 'success', 'message': 'Movie added successfully.', 'id': movie_id})

    
@app.route('/save', methods=['POST'])
def save():    
    movie_data = json.loads(request.json)

    csv_file_path = './assets/CSV/Catalog.csv'

    # Load existing movies
    existing_movies = []
    try:
        with open(csv_file_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_movies.append(row)
    except FileNotFoundError:
        pass

    # Calculate the next movie ID based on existing data
    next_movie_id = calculate_next_movie_id(existing_movies)

    # Add the ID to the movie data
    movie_data['id'] = next_movie_id

    # Check for duplicates by comparing IDs
    if not any(movie['id'] == next_movie_id for movie in existing_movies):
        existing_movies.append(movie_data)

        # Write to CSV
        with open(csv_file_path, mode='w', newline='', encoding='utf-8') as f:
            fieldnames = movie_data.keys()
            writer = csv.DictWriter(f, fieldnames=fieldnames)

            writer.writeheader()
            for movie in existing_movies:
                writer.writerow(movie)

        return jsonify({"status": "success", "movie_id": next_movie_id})
    else:
        return jsonify({"status": "duplicate movie ID"})

def calculate_next_movie_id(existing_movies):
    # Calculate the next movie ID based on existing data
    if not existing_movies:
        return 1  # If no existing movies, start with ID 1
    else:
        # Find the maximum movie ID and increment it
        
        existing_movies[0]['id']
        max_id = max(int(movie['id']) for movie in existing_movies)
        return max_id + 1


def read_movie_data_from_csv(csv_file_path):
    movie_data = []

    try:
        with open(csv_file_path, mode='r', encoding='utf-8') as file:
            csv_reader = csv.DictReader(file)

            for row in csv_reader:
                movie_data.append(row)
    except FileNotFoundError:
        pass

    return movie_data

@app.route('/delete_movie/<int:movie_id>', methods=['POST'])
def delete_movie(movie_id):
    csv_file_path = './assets/CSV/Catalog.csv'
    movie_data = read_movie_data_from_csv(csv_file_path)

    # Load existing movies
    existing_movies = []
    try:
        with open(csv_file_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_movies.append(row)
    except FileNotFoundError:
        return jsonify({"success": False, "message": "CSV file not found."})

    # Check if the movie with the specified ID exists
    movie_to_delete = None
    for movie in existing_movies:
        if int(movie['id']) == movie_id:
            movie_to_delete = movie
            break

    if movie_to_delete:
        # Remove the movie from the list
        existing_movies.remove(movie_to_delete)

        # Write the updated list of movies back to the CSV
        with open(csv_file_path, mode='w', newline='', encoding='utf-8') as f:
            fieldnames = movie_data[0].keys()
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for movie in existing_movies:
                writer.writerow(movie)

        return jsonify({"success": True, "message": "Movie deleted successfully."})
    else:
        return jsonify({"success": False, "message": "Movie not found."})


@app.route('/')
def home():
    return render_template('index.html')

@app.route('/catalog')
def show_catalog():
    movies = []
    with open('./assets/CSV/Catalog.csv', 'r', encoding='utf-8') as f:
        csv_reader = csv.DictReader(f)
        for row in csv_reader:
            row['cast'] = ast.literal_eval(row['cast'])  # Converts string to list
            row['certificates'] = ast.literal_eval(row['certificates'])
            row['director'] = ast.literal_eval(row['director'])
            row['genres'] = ast.literal_eval(row['genres'])
            row['runtime'] = ast.literal_eval(row['runtime'])
            movies.append(row)
    genres = extract_unique_items('./assets/CSV/Catalog.csv', 'genres')
    
    return render_template('Catalog.html', movies=movies, genres=genres)

@app.route('/add_movies')
def add_movies():
    return render_template('Add_Movies.html')

# Other routes for AJAX, etc.

@app.route('/add_book')
def add_book():
    return render_template('add_book.html')


def extract_book_info(soup):
    book_info = {}
    
    try:
        book_info['rating'] = soup.find('span', {'itemprop': 'ratingValue'}).text
    except AttributeError:
        book_info['rating'] = 'Rating not available'
    
    try:
        book_info['image_url'] = soup.find('img', {'itemprop': 'image'})['src']
    except TypeError:
        book_info['image_url'] = 'Image not available'
        
    try:
        book_info['publish_date'] = soup.find('span', {'itemprop': 'datePublished'}).text
    except AttributeError:
        book_info['publish_date'] = ''
        
    try:
        book_info['publisher'] = soup.find('a', {'itemprop': 'publisher'}).text
    except AttributeError:
        book_info['publisher'] = ''
        
    try:
        book_info['language'] = soup.find('span', {'itemprop': 'inLanguage'}).text
    except AttributeError:
        book_info['language'] = ''
        
    try:
        book_info['pages'] = soup.find('span', {'itemprop': 'numberOfPages'}).text
    except AttributeError:
        book_info['pages'] = ''

    try:
        amazon_price_element = soup.find('span', {'name': 'price'}).text
        book_info['amazon_price'] = extract_cash_amount(amazon_price_element)
    except AttributeError:
        book_info['amazon_price'] = 'Price not available'

    try:
        description_paragraph = soup.find('div', {'class': 'book-description-content restricted-view'}).find('p').text
        book_info['description'] = description_paragraph
    except AttributeError:
        book_info['description'] = ''

    try:
        book_info['title'] = soup.find('h1', {'class': 'work-title', 'itemprop': 'name'}).text
    except AttributeError:
        book_info['title'] = 'Title not available'

    try:
        book_info['isbn'] = soup.find('dd', {'class': 'object', 'itemprop': 'isbn'}).text.strip()
    except AttributeError:
        book_info['isbn'] = 'ISBN not available'
    try:
        author_element = soup.find('a', {'itemprop': 'author'})
        book_info['author'] = author_element.text if author_element else 'Author not available'
    except AttributeError:
        book_info['author'] = 'Author not available'

    return book_info


@app.route('/get_info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url')
    response = requests.get(url)
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        book_info = extract_book_info(soup)
        
        return jsonify(book_info)
    
    return jsonify({"error": "Could not fetch info"})


#9781632150530
#
@app.route('/save_book', methods=['POST'])
def save_book():
    request_data = request.json
    isbn_to_match = request_data.get('isbn', None)

    try:
        with open('./assets/JSON/Books.json', 'r') as f:
            existing_data = json.load(f)
    except FileNotFoundError:
        existing_data = {'Books': []}

    found = False

    for index, book in enumerate(existing_data['Books']):
        if book.get('isbn') == isbn_to_match:
            found = True
            existing_data['Books'][index] = {**book, **request_data}  # Merging dictionaries
            break

    if not found:
        new_id = len(existing_data['Books']) + 1
        request_data['id'] = new_id  # Generate a new incremental ID
        existing_data['Books'].append(request_data)

    with open('./assets/JSON/Books.json', 'w') as f:
        json.dump(existing_data, f, indent=4)

    return jsonify({"message": "Book saved successfully", "id": new_id if not found else book.get('id')})

@app.route('/book/<int:book_id>', methods=['GET'])
def get_book(book_id):
    print(f"Searching for book with ID: {book_id}")
    # Read the existing data from the JSON file
    with open('./assets/JSON/Books.json', 'r') as f:
        file_contents = json.load(f)

    # The list of books will be available in file_contents['Books']
    books = file_contents['Books']

    # Look for the book with the matching ID
    for book in books:
        print(book['id'])
        if book['id'] == book_id:
            return render_template('view_book.html', book=book)

    return jsonify({"error": "Book not found"}), 404

# Flask route to list all books
@app.route('/books', methods=['GET'])
def list_books():
    try:
        with open('./assets/JSON/Books.json', 'r') as f:
            file_contents = json.load(f)
    except FileNotFoundError:
        return "Books file not found", 404
    
    books = file_contents['Books']
    return render_template('list_books.html', books=books)

# This function can be placed wherever you're keeping utility functions
def extract_cash_amount(text):
    import re
    try:
        cash_amount = re.search(r'(\$\d+\.\d+)', text).group(1)
    except AttributeError:
        cash_amount = 'Cash amount not found'
    return cash_amount

def run():
    app.run(host='0.0.0.0', port=5002, debug=True)

if __name__ == '__main__':
    run()