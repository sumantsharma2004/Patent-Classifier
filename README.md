# QKD Patent Analyzer

A Streamlit-based application for analyzing patents to determine their relevance to Quantum Key Distribution (QKD) systems using Azure OpenAI.

## Features

- **Multi-format File Support**: Upload CSV, TSV, or Excel files
- **Multi-row Record Consolidation**: Automatically merge patent descriptions split across multiple rows
- **Customizable Prompts**: Use default QKD analysis prompt or create your own
- **Flexible Column Mapping**: Map your file's columns to required fields
- **Azure OpenAI Integration**: Powered by GPT-4o for intelligent patent analysis
- **Rich Results Visualization**: Charts, metrics, and detailed patent information
- **Export Results**: Download analysis results as Excel or CSV

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Azure OpenAI Credentials

Create a `.env` file in the project root directory:

```bash
cp .env.example .env
```

Edit the `.env` file with your actual Azure OpenAI credentials:

```env
AZURE_API_KEY=your_api_key_here
AZURE_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_API_VERSION=2025-01-01-preview
AZURE_MODEL=gpt-4o
```

**Important**: Never commit the `.env` file to version control! It's already included in `.gitignore`.

## Usage

### Running the Streamlit App

#### Option 1: Using the batch file (Windows)
```bash
run_streamlit.bat
```

#### Option 2: Using command line
```bash
streamlit run qkd_patent_analyzer_streamlit.py
```

### Running the Command-Line Script

For batch processing without the UI:

```bash
python qkd_patent_analyzer.py
```

**Note**: Update the `excel_file` variable in the script to point to your input file.

## How to Use the Streamlit App

1. **Configure Azure OpenAI Settings** (in sidebar):
   - Credentials are automatically loaded from `.env` file
   - You can override them in the UI if needed

2. **Upload Your Patent Data**:
   - Supported formats: CSV, TSV, Excel (.xlsx, .xls)
   - Enable "Consolidate multi-row patent records" if your data has descriptions split across rows

3. **Map Columns**:
   - Select which columns correspond to:
     - Publication Number (required)
     - Title (required)
     - Abstract (required)
     - Claims (optional)
     - Description (optional)

4. **Configure Prompt** (optional):
   - Use the default QKD analysis prompt
   - Or customize it with placeholders: `{publication_number}`, `{title}`, `{abstract}`, `{claims}`, `{description}`

5. **Start Analysis**:
   - Click "Start Analysis" button
   - Monitor progress as patents are analyzed

6. **View Results**:
   - Summary statistics and charts
   - Detailed breakdown of relevant patents
   - Full results table

7. **Download Results**:
   - Export as Excel or CSV
   - Includes relevance scores, confidence levels, and reasoning

## Multi-row Record Consolidation

If your patent data has descriptions split across multiple rows (common with patent database exports), enable the "Consolidate multi-row patent records" option. The app will:

1. Detect rows starting with publication numbers (e.g., `US1234567`, `CN987654`)
2. Merge continuation rows into the description field
3. Create clean, single-row records for each patent

You can download the consolidated data before running the analysis.

## Files

- `qkd_patent_analyzer_streamlit.py` - Streamlit web application
- `qkd_patent_analyzer.py` - Command-line batch processing script
- `.env` - Your Azure OpenAI credentials (not committed to git)
- `.env.example` - Template for environment variables
- `requirements.txt` - Python dependencies
- `run_streamlit.bat` - Windows batch file to launch the app

## Security Notes

- **Never commit `.env` file**: It contains sensitive API keys
- **Use `.env.example`**: Share this template instead of actual credentials
- **API Key Protection**: API keys are masked in the UI (password field)

## License

This tool is for internal use with authorized Azure OpenAI credentials.
